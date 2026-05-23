"""
Favorite lists management for the Kroger MCP server.

Provides core functions for managing named favorite lists and items,
integrating with the pantry system for smart reordering.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from kroger_mcp.auth.dependencies import mcp_user_id

from .database import ensure_initialized, get_db_cursor


def _resolve_user_id(user_id: str | None) -> str:
    """Resolve user_id for user-scoped queries.

    HTTP route handlers always pass user_id from the session. MCP/script
    callers may pass None; we fall back to `mcp_user_id()` which honors
    KROGER_MCP_USER_ID per Claude Desktop profile, then
    KROGER_MCP_DEFAULT_USER_ID.
    """
    return user_id if user_id is not None else mcp_user_id()

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


def get_all_favorite_product_ids() -> set:
    """
    Get all product IDs across all favorite lists.

    Returns a set of product_ids for fast O(1) lookup when checking
    if a product is in any favorites list.

    Returns:
        Set of product_id strings
    """
    ensure_initialized()

    with get_db_cursor() as cursor:
        cursor.execute("SELECT DISTINCT product_id FROM favorite_list_items")
        return {row["product_id"] for row in cursor.fetchall()}


# ========== List Management ==========


def create_list(
    name: str,
    description: str | None = None,
    list_type: str = "custom",
    reorder_weeks: int | None = None,
    user_id: str | None = None,
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


def get_lists(user_id: str | None = None) -> list[dict[str, Any]]:
    """Get all favorite lists owned by `user_id` with item counts and reorder status."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    _ensure_default_list_for_user(owner)

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
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM favorite_lists WHERE user_id = ?", (user_id,)
        )
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


def get_list(list_id: str, user_id: str | None = None) -> dict[str, Any] | None:
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
    list_id: str, new_name: str | None = None, new_description: str | None = None
) -> dict[str, Any]:
    """
    Rename a list or update its description.

    Args:
        list_id: The list ID
        new_name: New name (optional)
        new_description: New description (optional)

    Returns:
        Success status
    """
    ensure_initialized()

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
            params.append(list_id)

            cursor.execute(
                f"""
                UPDATE favorite_lists
                SET {', '.join(updates)}
                WHERE id = ?
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


def delete_list(list_id: str, user_id: str | None = None) -> dict[str, Any]:
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

        cursor.execute(
            "DELETE FROM favorite_lists WHERE id = ? AND user_id = ?", (list_id, owner)
        )

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
) -> dict[str, Any]:
    """
    Add a product to a favorite list.

    Args:
        list_id: The list ID
        product_id: Kroger product ID
        description: Product description
        brand: Product brand (optional)
        default_quantity: Default quantity when ordering
        preferred_modality: PICKUP or DELIVERY
        notes: Optional notes
        min_stock_percent: Reorder if pantry < this % (None = use global threshold)
        min_stock_quantity: Target on-hand unit count (None = not tracked)
        current_stock_quantity: Actual on-hand count (None = not tracked)

    Returns:
        Success status
    """
    ensure_initialized()

    # Verify list exists
    lst = get_list(list_id)
    if not lst:
        return {"success": False, "error": f"List '{list_id}' not found"}

    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO favorite_list_items
                (list_id, product_id, description, brand, default_quantity,
                 preferred_modality, notes, min_stock_percent, min_stock_quantity,
                 current_stock_quantity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def bulk_add_to_list(list_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Add multiple products to a favorite list in one operation.

    Args:
        list_id: The list ID
        items: List of items, each with:
            - product_id (required): Kroger product ID
            - description (required): Product description
            - brand (optional): Product brand
            - default_quantity (optional): Default quantity (default 1)
            - preferred_modality (optional): PICKUP or DELIVERY (default PICKUP)
            - notes (optional): Notes

    Returns:
        Success status with counts of added/failed items
    """
    ensure_initialized()

    # Verify list exists
    lst = get_list(list_id)
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
                     current_stock_quantity)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def remove_from_list(list_id: str, product_id: str) -> dict[str, Any]:
    """
    Remove a product from a favorite list.

    Args:
        list_id: The list ID
        product_id: Kroger product ID

    Returns:
        Success status
    """
    ensure_initialized()

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
    user_id: str | None = None,
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
                        MIN(fli.current_stock_quantity) as current_stock_quantity
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
                    fli.current_stock_quantity
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


def update_list_item(list_id: str, product_id: str, **kwargs) -> dict[str, Any]:
    """
    Update an item in a favorite list.

    Args:
        list_id: The list ID
        product_id: Kroger product ID
        **kwargs: Fields to update (default_quantity, preferred_modality, notes)

    Returns:
        Success status
    """
    ensure_initialized()

    allowed_fields = {
        "default_quantity",
        "preferred_modality",
        "notes",
        "min_stock_percent",
        "min_stock_quantity",
        "current_stock_quantity",
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


def increment_times_ordered(list_id: str, product_ids: list[str]) -> None:
    """
    Increment the times_ordered counter for products that were ordered.

    Args:
        list_id: The list ID
        product_ids: List of product IDs that were ordered
    """
    ensure_initialized()

    if not product_ids:
        return

    placeholders = ", ".join("?" * len(product_ids))
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE favorite_list_items
            SET times_ordered = times_ordered + 1
            WHERE list_id = ? AND product_id IN ({placeholders})
            """,
            [list_id] + product_ids,
        )


# ========== Smart Features ==========


def get_items_needing_reorder(
    list_id: str = "default", pantry_threshold: int = 30
) -> dict[str, Any]:
    """
    Get items from a list that need reordering based on pantry levels.

    Args:
        list_id: The list ID
        pantry_threshold: Pantry level below which items need reorder

    Returns:
        Dict with items needing reorder
    """
    ensure_initialized()

    lst = get_list(list_id)
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


def update_list_schedule(list_id: str, reorder_weeks: int | None) -> dict[str, Any]:
    """
    Update the reorder schedule for an existing list.

    Args:
        list_id: The list ID
        reorder_weeks: Number of weeks between reorders (1-52), or None to disable

    Returns:
        Success status with updated reorder info
    """
    ensure_initialized()

    if list_id == "default":
        return {"success": False, "error": "Cannot modify schedule for the default list"}

    # Validate reorder_weeks
    if reorder_weeks is not None:
        if not isinstance(reorder_weeks, int) or reorder_weeks < 1 or reorder_weeks > 52:
            return {
                "success": False,
                "error": "reorder_weeks must be between 1 and 52, or None to disable",
            }

    with get_db_cursor() as cursor:
        # Check if list exists and get current last_ordered_at
        cursor.execute("SELECT last_ordered_at FROM favorite_lists WHERE id = ?", (list_id,))
        row = cursor.fetchone()

        if not row:
            return {"success": False, "error": f"List '{list_id}' not found"}

        last_ordered_at = row["last_ordered_at"]

        # Update the schedule
        cursor.execute(
            """
            UPDATE favorite_lists
            SET reorder_weeks = ?, updated_at = ?
            WHERE id = ?
            """,
            (reorder_weeks, datetime.now().isoformat(), list_id),
        )

    # Calculate new reorder status
    reorder_status = _calculate_reorder_status(last_ordered_at, reorder_weeks)

    return {
        "success": True,
        "list_id": list_id,
        "reorder_weeks": reorder_weeks,
        "reorder_status": reorder_status,
    }


def get_low_stock_items(list_id: str) -> dict[str, Any]:
    """
    Return items from a favorites list that are below their user-defined minimum stock.

    Only includes items that have at least one minimum configured
    (min_stock_percent or min_stock_quantity).

    Args:
        list_id: The list ID

    Returns:
        Dict with low_stock_items list and summary counts
    """
    ensure_initialized()

    lst = get_list(list_id)
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


def mark_list_ordered(list_id: str) -> dict[str, Any]:
    """
    Mark a list as ordered, updating the last_ordered_at timestamp.

    This should be called after successfully ordering items from a list.

    Args:
        list_id: The list ID

    Returns:
        Success status with reorder info
    """
    ensure_initialized()

    now = datetime.now().isoformat()

    with get_db_cursor() as cursor:
        # Get current reorder_weeks
        cursor.execute(
            "SELECT reorder_weeks, last_ordered_at FROM favorite_lists WHERE id = ?", (list_id,)
        )
        row = cursor.fetchone()

        if not row:
            return {"success": False, "error": f"List '{list_id}' not found"}

        reorder_weeks = row["reorder_weeks"]
        previous_ordered_at = row["last_ordered_at"]

        # Calculate if it was overdue before marking
        previous_status = _calculate_reorder_status(previous_ordered_at, reorder_weeks)
        was_overdue = previous_status.get("is_overdue", False)

        # Update last_ordered_at
        cursor.execute(
            """
            UPDATE favorite_lists
            SET last_ordered_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, list_id),
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
