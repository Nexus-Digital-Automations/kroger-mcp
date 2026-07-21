"""
Favorite lists management for the Kroger MCP server.

Provides core functions for managing named favorite lists and items,
integrating with the pantry system for smart reordering.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from kroger_mcp.cache import cache_read_through

from .database import ensure_initialized, get_db_cursor

# Default "days between buys" used to flag a snack as stale when the item has
# no explicit typical_gap_days set. Snacks have no fixed reorder schedule, so
# this is only a pre-tick heuristic for the pre-cart check-up — never a hard
# cadence like favorite_lists.reorder_weeks.
SNACK_DEFAULT_GAP_DAYS = 21
SNACK_PANTRY_LOW_PERCENT = 30


def _resolve_user_id(user_id: str) -> str:
    """Identity passthrough, kept so this module's ~20 call sites don't need a
    mechanical rename. Every public function here now requires `user_id: str`
    (its caller resolves it at the MCP/web boundary) — there's no None case
    left to resolve.
    """
    return user_id


# ========== Helper Functions ==========


def _calculate_reorder_status(
    last_ordered_at: str | None, reorder_weeks: int | None
) -> dict[str, Any]:
    """
    Calculate the reorder status for a list based on schedule.

    Args:
        last_ordered_at: ISO timestamp of last order, or None
        reorder_weeks: Number of weeks between reorders, or None/0 for no schedule

    Returns:
        Dict with reorder status info
    """
    if reorder_weeks is None or reorder_weeks == 0:
        return {"has_schedule": False}

    if last_ordered_at is None:
        return {
            "has_schedule": True,
            "reorder_weeks": reorder_weeks,
            "last_ordered_at": None,
            "next_due_date": None,
            "days_until_due": None,
            "status": "never_ordered",
            "is_overdue": True,
        }

    try:
        last_order = datetime.fromisoformat(last_ordered_at.replace("Z", "+00:00"))
        # Handle timezone-naive datetimes
        if last_order.tzinfo is not None:
            last_order = last_order.replace(tzinfo=None)
    except (ValueError, AttributeError):
        # If parsing fails, treat as never ordered
        return {
            "has_schedule": True,
            "reorder_weeks": reorder_weeks,
            "last_ordered_at": last_ordered_at,
            "next_due_date": None,
            "days_until_due": None,
            "status": "never_ordered",
            "is_overdue": True,
        }

    next_due = last_order + timedelta(weeks=reorder_weeks)
    now = datetime.now()
    days_until_due = (next_due - now).days

    if days_until_due < 0:
        status = "overdue"
        is_overdue = True
    elif days_until_due <= 3:
        status = "due_soon"
        is_overdue = False
    else:
        status = "on_schedule"
        is_overdue = False

    return {
        "has_schedule": True,
        "reorder_weeks": reorder_weeks,
        "last_ordered_at": last_ordered_at,
        "next_due_date": next_due.isoformat(),
        "days_until_due": days_until_due,
        "days_overdue": abs(days_until_due) if days_until_due < 0 else 0,
        "status": status,
        "is_overdue": is_overdue,
    }


def get_all_favorite_product_ids(*, user_id: str) -> set:
    """
    Get all product IDs across this user's favorite lists.

    Returns a set of product_ids for fast O(1) lookup when checking
    if a product is in any of this user's favorites lists.

    Called once per product search to annotate results. Cached in Redis for a
    short window (60s) so repeated searches skip the table scan; staleness is
    cosmetic (a heart icon lagging at most 60s) and self-heals on TTL.

    Args:
        user_id: Owner whose favorite lists to check.

    Returns:
        Set of product_id strings
    """
    ensure_initialized()

    def _load() -> list[str]:
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT fli.product_id
                FROM favorite_list_items fli
                JOIN favorite_lists fl ON fli.list_id = fl.id
                WHERE fl.user_id = ?
                """,
                (user_id,),
            )
            return [row["product_id"] for row in cursor.fetchall()]

    return set(cache_read_through(f"fav:all_product_ids:{user_id}", 60, _load))


# ========== List Management ==========


def create_list(
    name: str,
    description: str | None = None,
    list_type: str = "custom",
    reorder_weeks: int | None = None,
    *, user_id: str,
) -> dict[str, Any]:
    """
    Create a new favorite list owned by `user_id`.

    Args:
        name: List name (must be unique per user)
        description: Optional description
        list_type: Type of list ('custom', 'weekly', 'monthly', 'seasonal')
        reorder_weeks: Number of weeks between reorders (None = no schedule)
        user_id: Owner. None resolves to the migration-installed default user.
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if reorder_weeks is not None:
        if not isinstance(reorder_weeks, int) or reorder_weeks < 1 or reorder_weeks > 52:
            return {"success": False, "error": "reorder_weeks must be between 1 and 52"}

    list_id = f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}"

    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO favorite_lists (id, name, description, list_type, reorder_weeks, user_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (list_id, name, description, list_type, reorder_weeks, owner),
            )

            # Calculate initial reorder status
            reorder_status = _calculate_reorder_status(None, reorder_weeks)

            return {
                "success": True,
                "list_id": list_id,
                "name": name,
                "list_type": list_type,
                "reorder_weeks": reorder_weeks,
                "reorder_status": reorder_status,
            }
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return {"success": False, "error": f"A list named '{name}' already exists"}
        return {"success": False, "error": str(e)}


def get_lists(user_id: str) -> list[dict[str, Any]]:
    """Get all favorite lists owned by `user_id` with item counts and reorder status."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    _ensure_default_list_for_user(owner)
    _ensure_snacks_list_for_user(owner)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
                fl.id,
                fl.name,
                fl.description,
                fl.list_type,
                fl.reorder_weeks,
                fl.last_ordered_at,
                fl.created_at,
                fl.updated_at,
                COUNT(fli.product_id) as item_count
            FROM favorite_lists fl
            LEFT JOIN favorite_list_items fli ON fl.id = fli.list_id
            WHERE fl.user_id = ?
            GROUP BY fl.id
            ORDER BY
                CASE WHEN fl.name = 'My Favorites' THEN 0 ELSE 1 END,
                fl.name
            """,
            (owner,),
        )
        rows = cursor.fetchall()

    results = []
    for row in rows:
        reorder_status = _calculate_reorder_status(row["last_ordered_at"], row["reorder_weeks"])

        results.append(
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "list_type": row["list_type"],
                "item_count": row["item_count"],
                "reorder_weeks": row["reorder_weeks"],
                "last_ordered_at": row["last_ordered_at"],
                "reorder_status": reorder_status,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "is_default": row["id"] == "default" or row["name"] == "My Favorites",
            }
        )

    return results


def _ensure_default_list_for_user(user_id: str) -> None:
    """Lazily create 'My Favorites' for a user that has no lists yet."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS cnt FROM favorite_lists WHERE user_id = ?", (user_id,))
        if cursor.fetchone()["cnt"] > 0:
            return
        new_id = f"default-{uuid.uuid4().hex[:8]}"
        cursor.execute(
            """
            INSERT INTO favorite_lists (id, name, description, list_type, user_id)
            VALUES (?, 'My Favorites', 'Default favorites list', 'custom', ?)
            """,
            (new_id, user_id),
        )


def _ensure_snacks_list_for_user(user_id: str) -> str:
    """Lazily create the built-in 'Snacks' list for a user; return its id.

    Snacks are favorites eaten on no schedule, so the list carries no
    reorder_weeks. The list is identified by list_type='snacks' (not by name),
    so a renamed list keeps its snack behavior.
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT id FROM favorite_lists WHERE user_id = ? AND list_type = 'snacks' LIMIT 1",
            (user_id,),
        )
        existing = cursor.fetchone()
        if existing:
            return existing["id"]
        new_id = f"snacks-{uuid.uuid4().hex[:8]}"
        cursor.execute(
            """
            INSERT INTO favorite_lists (id, name, description, list_type, user_id)
            VALUES (?, 'Snacks', 'Snacks eaten on no schedule — checked before each cart', 'snacks', ?)
            """,
            (new_id, user_id),
        )
        return new_id


def get_list(list_id: str, user_id: str) -> dict[str, Any] | None:
    """Get a single list by ID, only if it belongs to `user_id`.

    Returns None when the list doesn't exist OR belongs to a different user
    (treated identically so callers can't distinguish "missing" from "forbidden").
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
                fl.id,
                fl.name,
                fl.description,
                fl.list_type,
                fl.reorder_weeks,
                fl.last_ordered_at,
                fl.created_at,
                fl.updated_at,
                COUNT(fli.product_id) as item_count
            FROM favorite_lists fl
            LEFT JOIN favorite_list_items fli ON fl.id = fli.list_id
            WHERE fl.id = ? AND fl.user_id = ?
            GROUP BY fl.id
            """,
            (list_id, owner),
        )
        row = cursor.fetchone()

    if not row:
        return None

    reorder_status = _calculate_reorder_status(row["last_ordered_at"], row["reorder_weeks"])

    item_count = row["item_count"]
    if row["id"] == "default":
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(DISTINCT product_id) FROM favorite_list_items")
            item_count = cursor.fetchone()[0]

    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "list_type": row["list_type"],
        "item_count": item_count,
        "reorder_weeks": row["reorder_weeks"],
        "last_ordered_at": row["last_ordered_at"],
        "reorder_status": reorder_status,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "is_default": row["id"] == "default",
    }


def rename_list(
    list_id: str,
    new_name: str | None = None,
    new_description: str | None = None,
    *, user_id: str,
) -> dict[str, Any]:
    """Rename a list or update its description, only if it belongs to `user_id`."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if list_id == "default":
        return {"success": False, "error": "Cannot rename the default list"}

    if not new_name and new_description is None:
        return {"success": False, "error": "Must provide new_name or new_description"}

    try:
        with get_db_cursor() as cursor:
            updates = []
            params = []

            if new_name:
                updates.append("name = ?")
                params.append(new_name)

            if new_description is not None:
                updates.append("description = ?")
                params.append(new_description)

            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.extend([list_id, owner])

            cursor.execute(
                f"""
                UPDATE favorite_lists
                SET {', '.join(updates)}
                WHERE id = ? AND user_id = ?
                """,
                params,
            )

            if cursor.rowcount == 0:
                return {"success": False, "error": f"List '{list_id}' not found"}

            return {"success": True, "list_id": list_id}
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return {"success": False, "error": f"A list named '{new_name}' already exists"}
        return {"success": False, "error": str(e)}


def delete_list(list_id: str, user_id: str) -> dict[str, Any]:
    """Delete a list and its items, only if it belongs to `user_id`."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if list_id == "default":
        return {"success": False, "error": "Cannot delete the default list"}

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM favorite_list_items WHERE list_id = ?", (list_id,)
        )
        item_count = cursor.fetchone()["cnt"]

        cursor.execute("DELETE FROM favorite_lists WHERE id = ? AND user_id = ?", (list_id, owner))

        if cursor.rowcount == 0:
            return {"success": False, "error": f"List '{list_id}' not found"}

        return {"success": True, "list_id": list_id, "items_deleted": item_count}


# ========== Item Management ==========


def add_to_list(
    list_id: str,
    product_id: str,
    description: str,
    brand: str | None = None,
    default_quantity: int = 1,
    preferred_modality: str = "PICKUP",
    notes: str | None = None,
    min_stock_percent: int | None = None,
    min_stock_quantity: int | None = None,
    current_stock_quantity: int | None = None,
    typical_gap_days: int | None = None,
    *, user_id: str,
) -> dict[str, Any]:
    """Add a product to a favorite list, only if the list belongs to `user_id`."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    lst = get_list(list_id, user_id=owner)
    if not lst:
        return {"success": False, "error": f"List '{list_id}' not found"}

    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO favorite_list_items
                (list_id, product_id, description, brand, default_quantity,
                 preferred_modality, notes, min_stock_percent, min_stock_quantity,
                 current_stock_quantity, typical_gap_days)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    list_id,
                    product_id,
                    description,
                    brand,
                    default_quantity,
                    preferred_modality,
                    notes,
                    min_stock_percent,
                    min_stock_quantity,
                    current_stock_quantity,
                    typical_gap_days,
                ),
            )

            # Update list's updated_at
            cursor.execute(
                "UPDATE favorite_lists SET updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), list_id),
            )

            return {
                "success": True,
                "list_id": list_id,
                "product_id": product_id,
                "description": description,
            }
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return {
                "success": False,
                "error": f"Product '{product_id}' is already in list '{lst['name']}'",
            }
        return {"success": False, "error": str(e)}


def bulk_add_to_list(
    list_id: str,
    items: list[dict[str, Any]],
    user_id: str,
) -> dict[str, Any]:
    """Add multiple products in one operation, only if the list belongs to `user_id`."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    lst = get_list(list_id, user_id=owner)
    if not lst:
        return {"success": False, "error": f"List '{list_id}' not found"}

    added = []
    failed = []

    with get_db_cursor() as cursor:
        for item in items:
            product_id = item.get("product_id")
            description = item.get("description")

            if not product_id or not description:
                failed.append(
                    {
                        "product_id": product_id,
                        "error": "Missing required field: product_id or description",
                    }
                )
                continue

            try:
                cursor.execute(
                    """
                    INSERT INTO favorite_list_items
                    (list_id, product_id, description, brand, default_quantity,
                     preferred_modality, notes, min_stock_percent, min_stock_quantity,
                     current_stock_quantity, typical_gap_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        list_id,
                        product_id,
                        description,
                        item.get("brand"),
                        item.get("default_quantity", 1),
                        item.get("preferred_modality", "PICKUP"),
                        item.get("notes"),
                        item.get("min_stock_percent"),
                        item.get("min_stock_quantity"),
                        item.get("current_stock_quantity"),
                        item.get("typical_gap_days"),
                    ),
                )
                added.append({"product_id": product_id, "description": description})
            except Exception as e:
                if "UNIQUE constraint" in str(e):
                    failed.append({"product_id": product_id, "error": "Already in list"})
                else:
                    failed.append({"product_id": product_id, "error": str(e)})

        # Update list's updated_at if any items were added
        if added:
            cursor.execute(
                "UPDATE favorite_lists SET updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), list_id),
            )

    return {
        "success": len(added) > 0,
        "list_id": list_id,
        "list_name": lst["name"],
        "added": added,
        "failed": failed,
        "added_count": len(added),
        "failed_count": len(failed),
    }


def remove_from_list(
    list_id: str,
    product_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Remove a product from a list, only if the list belongs to `user_id`."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if not get_list(list_id, user_id=owner):
        return {"success": False, "error": f"List '{list_id}' not found"}

    with get_db_cursor() as cursor:
        cursor.execute(
            "DELETE FROM favorite_list_items WHERE list_id = ? AND product_id = ?",
            (list_id, product_id),
        )

        if cursor.rowcount == 0:
            return {
                "success": False,
                "error": f"Product '{product_id}' not found in list '{list_id}'",
            }

        # Update list's updated_at
        cursor.execute(
            "UPDATE favorite_lists SET updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), list_id),
        )

        return {"success": True, "list_id": list_id, "product_id": product_id}


def get_list_items(
    list_id: str,
    include_pantry_status: bool = True,
    sort_by: str = "description",
    *, user_id: str,
) -> dict[str, Any]:
    """Get items in a favorite list, only if it belongs to `user_id`.

    The pantry-status JOIN is also constrained to the user's own pantry rows
    so we don't leak other users' inventory levels through this endpoint.
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    lst = get_list(list_id, user_id=owner)
    if not lst:
        return {"success": False, "error": f"List '{list_id}' not found"}

    # Determine sort order
    sort_column = {
        "description": "fli.description",
        "times_ordered": "fli.times_ordered DESC",
        "added_at": "fli.added_at DESC",
    }.get(sort_by, "fli.description")

    # Aggregate sort uses aliases (GROUP BY query)
    agg_sort_column = {
        "description": "description",
        "times_ordered": "times_ordered DESC",
        "added_at": "added_at DESC",
    }.get(sort_by, "description")

    IS_DEFAULT = list_id == "default"

    with get_db_cursor() as cursor:
        if IS_DEFAULT:
            # Aggregate all items across all lists — live union view, deduped by product_id
            if include_pantry_status:
                cursor.execute(
                    f"""
                    SELECT
                        fli.product_id,
                        MIN(fli.description) as description,
                        MIN(fli.brand) as brand,
                        MAX(fli.default_quantity) as default_quantity,
                        MIN(fli.preferred_modality) as preferred_modality,
                        MIN(fli.notes) as notes,
                        MIN(fli.added_at) as added_at,
                        SUM(fli.times_ordered) as times_ordered,
                        MIN(fli.min_stock_percent) as min_stock_percent,
                        MIN(fli.min_stock_quantity) as min_stock_quantity,
                        MIN(fli.current_stock_quantity) as current_stock_quantity,
                        MAX(fli.last_ordered_at) as last_ordered_at,
                        MAX(fli.typical_gap_days) as typical_gap_days,
                        pi.level_percent,
                        pi.daily_depletion_rate,
                        pi.low_threshold
                    FROM favorite_list_items fli
                    LEFT JOIN pantry_items pi ON fli.product_id = pi.product_id
                    GROUP BY fli.product_id
                    ORDER BY {agg_sort_column}
                    """
                )
            else:
                cursor.execute(
                    f"""
                    SELECT
                        fli.product_id,
                        MIN(fli.description) as description,
                        MIN(fli.brand) as brand,
                        MAX(fli.default_quantity) as default_quantity,
                        MIN(fli.preferred_modality) as preferred_modality,
                        MIN(fli.notes) as notes,
                        MIN(fli.added_at) as added_at,
                        SUM(fli.times_ordered) as times_ordered,
                        MIN(fli.min_stock_percent) as min_stock_percent,
                        MIN(fli.min_stock_quantity) as min_stock_quantity,
                        MIN(fli.current_stock_quantity) as current_stock_quantity,
                        MAX(fli.last_ordered_at) as last_ordered_at,
                        MAX(fli.typical_gap_days) as typical_gap_days
                    FROM favorite_list_items fli
                    GROUP BY fli.product_id
                    ORDER BY {agg_sort_column}
                    """
                )
        elif include_pantry_status:
            cursor.execute(
                f"""
                SELECT
                    fli.product_id,
                    fli.description,
                    fli.brand,
                    fli.default_quantity,
                    fli.preferred_modality,
                    fli.notes,
                    fli.added_at,
                    fli.times_ordered,
                    fli.min_stock_percent,
                    fli.min_stock_quantity,
                    fli.current_stock_quantity,
                    fli.last_ordered_at,
                    fli.typical_gap_days,
                    pi.level_percent,
                    pi.daily_depletion_rate,
                    pi.low_threshold
                FROM favorite_list_items fli
                LEFT JOIN pantry_items pi ON fli.product_id = pi.product_id
                WHERE fli.list_id = ?
                ORDER BY {sort_column}
                """,
                (list_id,),
            )
        else:
            cursor.execute(
                f"""
                SELECT
                    fli.product_id,
                    fli.description,
                    fli.brand,
                    fli.default_quantity,
                    fli.preferred_modality,
                    fli.notes,
                    fli.added_at,
                    fli.times_ordered,
                    fli.min_stock_percent,
                    fli.min_stock_quantity,
                    fli.current_stock_quantity,
                    fli.last_ordered_at,
                    fli.typical_gap_days
                FROM favorite_list_items fli
                WHERE fli.list_id = ?
                ORDER BY {sort_column}
                """,
                (list_id,),
            )

        rows = cursor.fetchall()

    items = []
    for row in rows:
        min_pct = row["min_stock_percent"]
        min_qty = row["min_stock_quantity"]
        cur_qty = row["current_stock_quantity"]

        item = {
            "product_id": row["product_id"],
            "description": row["description"],
            "brand": row["brand"],
            "default_quantity": row["default_quantity"],
            "preferred_modality": row["preferred_modality"],
            "notes": row["notes"],
            "added_at": row["added_at"],
            "times_ordered": row["times_ordered"],
            "min_stock_percent": min_pct,
            "min_stock_quantity": min_qty,
            "current_stock_quantity": cur_qty,
            "last_ordered_at": row["last_ordered_at"],
            "typical_gap_days": row["typical_gap_days"],
        }

        if include_pantry_status:
            level = row["level_percent"]
            if level is not None:
                threshold = row["low_threshold"] or 20
                depletion = row["daily_depletion_rate"] or 0

                days_until_empty = None
                if depletion > 0:
                    days_until_empty = round(level / depletion, 1)

                item["pantry_status"] = {
                    "tracked": True,
                    "level_percent": level,
                    "days_until_empty": days_until_empty,
                    "is_low": level < threshold,
                    "needs_reorder": level < threshold,
                }
            else:
                item["pantry_status"] = {
                    "tracked": False,
                    "level_percent": None,
                    "needs_reorder": None,
                }

            # Compute per-item minimum stock status
            below_min_percent = min_pct is not None and (level is None or level < min_pct)
            below_min_quantity = min_qty is not None and (cur_qty is None or cur_qty < min_qty)
            item["below_min_percent"] = below_min_percent
            item["below_min_quantity"] = below_min_quantity
            item["needs_restock"] = below_min_percent or below_min_quantity

        items.append(item)

    return {"success": True, "list": lst, "items": items, "total_items": len(items)}


def update_list_item(
    list_id: str,
    product_id: str,
    user_id: str,
    **kwargs,
) -> dict[str, Any]:
    """Update an item in a favorite list, only if the list belongs to `user_id`."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if not get_list(list_id, user_id=owner):
        return {"success": False, "error": f"List '{list_id}' not found"}

    allowed_fields = {
        "default_quantity",
        "preferred_modality",
        "notes",
        "min_stock_percent",
        "min_stock_quantity",
        "current_stock_quantity",
        "typical_gap_days",
    }
    # Use `is not None` so 0 is a valid stock count but unprovided fields are skipped
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}

    if not updates:
        return {"success": False, "error": "No valid fields to update"}

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [list_id, product_id]

    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE favorite_list_items
            SET {set_clause}
            WHERE list_id = ? AND product_id = ?
            """,
            params,
        )

        if cursor.rowcount == 0:
            return {
                "success": False,
                "error": f"Product '{product_id}' not found in list '{list_id}'",
            }

        return {
            "success": True,
            "list_id": list_id,
            "product_id": product_id,
            "updated_fields": list(updates.keys()),
        }


def increment_times_ordered(
    list_id: str,
    product_ids: list[str],
    user_id: str,
) -> None:
    """Increment times_ordered for products, only if the list belongs to `user_id`."""
    ensure_initialized()

    if not product_ids:
        return

    if not get_list(list_id, user_id=_resolve_user_id(user_id)):
        return

    placeholders = ", ".join("?" * len(product_ids))
    now = datetime.now().isoformat()
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE favorite_list_items
            SET times_ordered = times_ordered + 1,
                last_ordered_at = ?
            WHERE list_id = ? AND product_id IN ({placeholders})
            """,
            [now, list_id] + product_ids,
        )


# ========== Smart Features ==========


def get_snacks_list_ids(user_id: str) -> list[str]:
    """Return the ids of the user's snack-type lists, ensuring one exists."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    _ensure_snacks_list_for_user(owner)
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT id FROM favorite_lists WHERE user_id = ? AND list_type = 'snacks'",
            (owner,),
        )
        return [row["id"] for row in cursor.fetchall()]


def _days_since(iso_timestamp: str | None) -> int | None:
    """Whole days between an ISO timestamp and now; None if missing/unparseable."""
    if not iso_timestamp:
        return None
    try:
        when = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if when.tzinfo is not None:
            when = when.replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None
    return (datetime.now() - when).days


def check_snacks(
    user_id: str,
    pantry_low_percent: int = SNACK_PANTRY_LOW_PERCENT,
) -> dict[str, Any]:
    """Build the pre-cart snack replenishment checklist.

    A snack is pre-ticked ("likely needs replenishing") when ANY holds:
      - it is pantry-tracked and below `pantry_low_percent`, or
      - it has never been ordered, or
      - days since last ordered >= its typical_gap_days (default 21).

    The user always confirms — nothing is added to any cart or list here.
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    snack_list_ids = get_snacks_list_ids(owner)
    if not snack_list_ids:
        return {
            "success": True,
            "snacks_list_ids": [],
            "candidates": [],
            "count": 0,
            "flagged_count": 0,
        }

    placeholders = ", ".join("?" * len(snack_list_ids))
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                fli.list_id,
                fli.product_id,
                fli.description,
                fli.brand,
                fli.default_quantity,
                fli.preferred_modality,
                fli.last_ordered_at,
                fli.typical_gap_days,
                pi.level_percent
            FROM favorite_list_items fli
            LEFT JOIN pantry_items pi ON fli.product_id = pi.product_id
            WHERE fli.list_id IN ({placeholders})
            ORDER BY fli.description
            """,
            snack_list_ids,
        )
        rows = cursor.fetchall()

    candidates = []
    for row in rows:
        level = row["level_percent"]
        gap = row["typical_gap_days"] or SNACK_DEFAULT_GAP_DAYS
        days_since = _days_since(row["last_ordered_at"])
        never_ordered = row["last_ordered_at"] is None

        pantry_low = level is not None and level < pantry_low_percent
        stale = days_since is not None and days_since >= gap
        pre_ticked = pantry_low or never_ordered or stale

        if pantry_low:
            reason = f"Pantry at {level}%"
        elif never_ordered:
            reason = "Never ordered yet"
        elif stale:
            reason = f"Last bought {days_since}d ago (≈ every {gap}d)"
        elif days_since is not None:
            reason = f"Bought {days_since}d ago"
        else:
            reason = "Recently ordered"

        candidates.append(
            {
                "list_id": row["list_id"],
                "product_id": row["product_id"],
                "description": row["description"],
                "brand": row["brand"],
                "default_quantity": row["default_quantity"],
                "preferred_modality": row["preferred_modality"],
                "pantry_level": level,
                "days_since_ordered": days_since,
                "typical_gap_days": gap,
                "never_ordered": never_ordered,
                "pre_ticked": pre_ticked,
                "reason": reason,
            }
        )

    return {
        "success": True,
        "snacks_list_ids": snack_list_ids,
        "candidates": candidates,
        "count": len(candidates),
        "flagged_count": sum(1 for c in candidates if c["pre_ticked"]),
    }


def mark_snacks_ordered(product_ids: list[str], user_id: str) -> int:
    """Stamp last_ordered_at + bump times_ordered for snacks just sent to cart.

    Scoped to the user's snack-type lists so a product that also lives in a
    scheduled list isn't disturbed. Returns the number of rows stamped.
    """
    ensure_initialized()
    if not product_ids:
        return 0
    owner = _resolve_user_id(user_id)
    snack_list_ids = get_snacks_list_ids(owner)
    if not snack_list_ids:
        return 0

    now = datetime.now().isoformat()
    list_ph = ", ".join("?" * len(snack_list_ids))
    prod_ph = ", ".join("?" * len(product_ids))
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE favorite_list_items
            SET last_ordered_at = ?, times_ordered = times_ordered + 1
            WHERE list_id IN ({list_ph}) AND product_id IN ({prod_ph})
            """,
            [now] + snack_list_ids + product_ids,
        )
        return cursor.rowcount


def get_items_needing_reorder(
    list_id: str = "default",
    pantry_threshold: int = 30,
    *, user_id: str,
) -> dict[str, Any]:
    """Get items needing reorder, only if the list belongs to `user_id`."""
    ensure_initialized()

    lst = get_list(list_id, user_id=_resolve_user_id(user_id))
    if not lst:
        return {"success": False, "error": f"List '{list_id}' not found"}

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
                fli.product_id,
                fli.description,
                fli.brand,
                fli.default_quantity,
                fli.preferred_modality,
                pi.level_percent,
                pi.daily_depletion_rate
            FROM favorite_list_items fli
            LEFT JOIN pantry_items pi ON fli.product_id = pi.product_id
            WHERE fli.list_id = ?
              AND (pi.level_percent IS NULL OR pi.level_percent < ?)
            ORDER BY COALESCE(pi.level_percent, 0) ASC
            """,
            (list_id, pantry_threshold),
        )
        rows = cursor.fetchall()

    items = []
    for row in rows:
        level = row["level_percent"]
        depletion = row["daily_depletion_rate"] or 0

        days_until_empty = None
        if level is not None and depletion > 0:
            days_until_empty = round(level / depletion, 1)

        items.append(
            {
                "product_id": row["product_id"],
                "description": row["description"],
                "brand": row["brand"],
                "default_quantity": row["default_quantity"],
                "preferred_modality": row["preferred_modality"],
                "pantry_level": level,
                "days_until_empty": days_until_empty,
                "in_pantry": level is not None,
            }
        )

    return {
        "success": True,
        "list": lst,
        "items_needing_reorder": items,
        "count": len(items),
        "threshold_used": pantry_threshold,
    }


def suggest_for_list(
    list_id: str | None = None,
    min_purchases: int = 3,
    min_frequency_score: float = 0.5,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Suggest products to add to favorites based on purchase history.

    Args:
        list_id: If provided, excludes items already in that list
        min_purchases: Minimum number of purchases to be suggested
        min_frequency_score: Minimum frequency score
        limit: Max suggestions to return

    Returns:
        List of suggested products
    """
    ensure_initialized()

    with get_db_cursor() as cursor:
        # Get products already in any list
        if list_id:
            cursor.execute(
                """
                SELECT DISTINCT product_id FROM favorite_list_items
                WHERE list_id = ?
                """,
                (list_id,),
            )
        else:
            cursor.execute("SELECT DISTINCT product_id FROM favorite_list_items")

        existing_products = {row["product_id"] for row in cursor.fetchall()}

        # Get frequently purchased products not in favorites
        cursor.execute(
            """
            SELECT
                p.product_id,
                p.description,
                p.brand,
                ps.total_purchases,
                ps.purchase_frequency_score,
                ps.avg_days_between_purchases,
                ps.last_purchase_date
            FROM products p
            JOIN product_statistics ps ON p.product_id = ps.product_id
            WHERE ps.total_purchases >= ?
              AND ps.purchase_frequency_score >= ?
            ORDER BY ps.purchase_frequency_score DESC, ps.total_purchases DESC
            LIMIT ?
            """,
            (min_purchases, min_frequency_score, limit * 3),  # Get extra to filter
        )
        rows = cursor.fetchall()

    suggestions = []
    for row in rows:
        if row["product_id"] not in existing_products:
            suggestions.append(
                {
                    "product_id": row["product_id"],
                    "description": row["description"],
                    "brand": row["brand"],
                    "total_purchases": row["total_purchases"],
                    "frequency_score": round(row["purchase_frequency_score"], 2),
                    "avg_days_between": (
                        round(row["avg_days_between_purchases"], 1)
                        if row["avg_days_between_purchases"]
                        else None
                    ),
                    "last_purchased": row["last_purchase_date"],
                }
            )
            if len(suggestions) >= limit:
                break

    return {
        "success": True,
        "suggestions": suggestions,
        "count": len(suggestions),
        "excluded_list": list_id,
    }


# ========== Reorder Schedule Management ==========


def update_list_schedule(
    list_id: str,
    reorder_weeks: int | None,
    user_id: str,
) -> dict[str, Any]:
    """Update the reorder schedule, only if the list belongs to `user_id`."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if list_id == "default":
        return {"success": False, "error": "Cannot modify schedule for the default list"}

    if reorder_weeks is not None:
        if not isinstance(reorder_weeks, int) or reorder_weeks < 1 or reorder_weeks > 52:
            return {
                "success": False,
                "error": "reorder_weeks must be between 1 and 52, or None to disable",
            }

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT last_ordered_at FROM favorite_lists WHERE id = ? AND user_id = ?",
            (list_id, owner),
        )
        row = cursor.fetchone()

        if not row:
            return {"success": False, "error": f"List '{list_id}' not found"}

        last_ordered_at = row["last_ordered_at"]

        cursor.execute(
            """
            UPDATE favorite_lists
            SET reorder_weeks = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (reorder_weeks, datetime.now().isoformat(), list_id, owner),
        )

    # Calculate new reorder status
    reorder_status = _calculate_reorder_status(last_ordered_at, reorder_weeks)

    return {
        "success": True,
        "list_id": list_id,
        "reorder_weeks": reorder_weeks,
        "reorder_status": reorder_status,
    }


def get_low_stock_items(
    list_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Return low-stock items, only if the list belongs to `user_id`."""
    ensure_initialized()

    lst = get_list(list_id, user_id=_resolve_user_id(user_id))
    if not lst:
        return {"success": False, "error": f"List '{list_id}' not found"}

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
                fli.product_id,
                fli.description,
                fli.brand,
                fli.default_quantity,
                fli.min_stock_percent,
                fli.min_stock_quantity,
                fli.current_stock_quantity,
                pi.level_percent
            FROM favorite_list_items fli
            LEFT JOIN pantry_items pi ON fli.product_id = pi.product_id
            WHERE fli.list_id = ?
              AND (fli.min_stock_percent IS NOT NULL OR fli.min_stock_quantity IS NOT NULL)
            ORDER BY fli.description
            """,
            (list_id,),
        )
        rows = cursor.fetchall()

    low_stock = []
    for row in rows:
        min_pct = row["min_stock_percent"]
        min_qty = row["min_stock_quantity"]
        cur_qty = row["current_stock_quantity"]
        level = row["level_percent"]

        below_min_percent = min_pct is not None and (level is None or level < min_pct)
        below_min_quantity = min_qty is not None and (cur_qty is None or cur_qty < min_qty)

        if not (below_min_percent or below_min_quantity):
            continue

        reasons = []
        if below_min_percent:
            reasons.append(f"Pantry {level if level is not None else 0}% < minimum {min_pct}%")
        if below_min_quantity:
            reasons.append(
                f"Have {cur_qty if cur_qty is not None else 0} units, minimum is {min_qty}"
            )

        low_stock.append(
            {
                "product_id": row["product_id"],
                "description": row["description"],
                "brand": row["brand"],
                "default_quantity": row["default_quantity"],
                "min_stock_percent": min_pct,
                "pantry_level": level,
                "min_stock_quantity": min_qty,
                "current_stock_quantity": cur_qty,
                "below_min_percent": below_min_percent,
                "below_min_quantity": below_min_quantity,
                "restock_reasons": reasons,
            }
        )

    return {
        "success": True,
        "list_id": list_id,
        "list_name": lst["name"],
        "low_stock_items": low_stock,
        "count": len(low_stock),
    }


def mark_list_ordered(
    list_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Mark a list as ordered, only if it belongs to `user_id`."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    now = datetime.now().isoformat()

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT reorder_weeks, last_ordered_at FROM favorite_lists WHERE id = ? AND user_id = ?",
            (list_id, owner),
        )
        row = cursor.fetchone()

        if not row:
            return {"success": False, "error": f"List '{list_id}' not found"}

        reorder_weeks = row["reorder_weeks"]
        previous_ordered_at = row["last_ordered_at"]

        previous_status = _calculate_reorder_status(previous_ordered_at, reorder_weeks)
        was_overdue = previous_status.get("is_overdue", False)

        cursor.execute(
            """
            UPDATE favorite_lists
            SET last_ordered_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, now, list_id, owner),
        )

    # Calculate new reorder status
    new_status = _calculate_reorder_status(now, reorder_weeks)

    return {
        "success": True,
        "list_id": list_id,
        "ordered_at": now,
        "was_overdue": was_overdue,
        "reorder_status": new_status,
    }
