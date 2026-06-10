"""
Pantry inventory tracking with auto-depletion.

Tracks estimated inventory levels using percentage-based tracking.
- Auto-depletes based on consumption rate analytics
- Manual adjustments supported
- Low inventory alerts when items drop below threshold
- Automatic expiration date tracking based on product category
"""

from datetime import datetime, timedelta
from typing import Any

from kroger_mcp.auth.dependencies import mcp_user_id

from .database import ensure_initialized, get_db_connection


def _resolve_user_id(user_id: str | None) -> str:
    """Resolve user_id for user-scoped queries.

    HTTP route handlers always pass user_id from the session. MCP/script
    callers may pass None; we fall back to `mcp_user_id()` which honors
    KROGER_MCP_USER_ID per Claude Desktop profile, then
    KROGER_MCP_DEFAULT_USER_ID. This means MCP profiles bound to different
    users see only their own data — no per-tool-dispatcher threading needed.
    """
    return user_id if user_id is not None else mcp_user_id()


# Shelf life in days for common categories
CATEGORY_SHELF_LIFE = {
    "routine": {
        # IMPORTANT: More specific (longer) keywords must come FIRST
        # because matching uses simple substring search
        # Frozen Foods (3-6 months) - MUST BE FIRST due to keyword priority
        "frozen vegetable": 240,
        "frozen fruit": 240,
        "frozen chicken": 180,
        "frozen beef": 180,
        "frozen pork": 180,
        "frozen fish": 120,
        "frozen seafood": 120,
        "frozen pizza": 120,
        "frozen meal": 90,
        "frozen dinner": 90,
        "ice cream": 60,
        "frozen": 120,  # Generic frozen (after specific items)
        # Dairy & Refrigerated
        "sour cream": 14,  # Must come before 'cream'
        "milk": 7,
        "dairy": 7,
        "cheese": 14,
        "yogurt": 14,
        "eggs": 21,
        "butter": 30,
        "cream": 7,
        # Bakery
        "bread": 5,
        "bakery": 5,
        "bagel": 5,
        "roll": 5,
        "tortilla": 7,
        # Fresh Meat & Seafood (Refrigerated)
        "ground": 2,  # Ground meat is shorter shelf life
        "deli": 5,
        "meat": 3,
        "poultry": 3,
        "chicken": 3,
        "beef": 3,
        "pork": 3,
        "seafood": 2,
        "fish": 2,
        # Produce
        "berries": 3,  # Must come before 'berry'
        "berry": 3,
        "lettuce": 5,
        "greens": 5,
        "salad": 5,
        "apple": 14,
        "orange": 14,
        "banana": 5,
        "vegetable": 7,  # Generic after specific
        "fruit": 7,  # Generic after specific
        "produce": 5,
    },
    "regular": {
        # Longer shelf life perishables
        "condiment": 90,
        "juice": 14,
        "refrigerated": 14,
        "sauce": 60,
        "ketchup": 180,
        "mustard": 180,
        "mayonnaise": 60,
    },
    "treat": None,  # Seasonal items typically don't expire quickly
}

# Default fallback (no category match)
DEFAULT_SHELF_LIFE = {
    "routine": 7,  # Weekly items default to 7 days
    "regular": 30,  # Monthly items default to 30 days
    "treat": None,  # Seasonal items no default
}


def calculate_depletion_rate(product_id: str, user_id: str | None = None) -> float:
    """
    Calculate daily depletion percentage from consumption rate analytics.

    Uses avg_days_between_purchases to estimate how quickly an item is used.

    Example:
    - Milk purchased every 7 days -> 100% / 7 = 14.3% per day
    - Eggs purchased every 14 days -> 100% / 14 = 7.1% per day

    Args:
        product_id: The product identifier
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Daily depletion rate as percentage (0-100)
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            SELECT avg_days_between_purchases
            FROM product_statistics
            WHERE product_id = ? AND user_id = ?
        """,
            (product_id, owner),
        )
        row = cursor.fetchone()

        if row and row["avg_days_between_purchases"]:
            avg_days = row["avg_days_between_purchases"]
            if avg_days > 0:
                return 100.0 / avg_days

        return 0.0  # No data, don't auto-deplete
    finally:
        conn.close()


def get_shelf_life_days(category: str, description: str) -> int | None:
    """
    Determine shelf life days for a product based on category and keywords.

    Scans product description for keyword matches to determine appropriate
    shelf life. Returns None for non-perishable items.

    Args:
        category: Product category ('routine', 'regular', 'treat')
        description: Product description to scan for keywords

    Returns:
        Shelf life in days, or None for non-perishables
    """
    if not category or not description:
        return None

    desc_lower = description.lower()
    category_map = CATEGORY_SHELF_LIFE.get(category, {})

    if category_map is None:
        return None

    # Check for keyword matches
    for keyword, days in category_map.items():
        if keyword in desc_lower:
            return days

    # Fallback to category default
    return DEFAULT_SHELF_LIFE.get(category)


def calculate_expiration_date(purchase_date: str, category: str, description: str) -> str | None:
    """
    Automatically calculate expiration date based on purchase date and category.

    Args:
        purchase_date: ISO date string (YYYY-MM-DD or full ISO timestamp)
        category: Product category ('routine', 'regular', 'treat')
        description: Product description for keyword matching

    Returns:
        ISO date string (YYYY-MM-DD) or None for non-perishables
    """
    shelf_life = get_shelf_life_days(category, description)
    if not shelf_life:
        return None

    try:
        # Handle both full ISO timestamps and date-only strings
        if "T" in purchase_date:
            purchase = datetime.fromisoformat(purchase_date).date()
        else:
            purchase = datetime.fromisoformat(purchase_date).date()
        expiration = purchase + timedelta(days=shelf_life)
        return expiration.isoformat()
    except (ValueError, TypeError):
        return None


def calculate_days_to_expiration(expiration_date: str | None) -> int | None:
    """
    Calculate days until expiration from ISO date string.

    Args:
        expiration_date: ISO date string (YYYY-MM-DD)

    Returns:
        Days until expiration (positive for future, negative for past),
        or None if no expiration date
    """
    if not expiration_date:
        return None

    try:
        exp_date = datetime.fromisoformat(expiration_date).date()
        today = datetime.now().date()
        delta = (exp_date - today).days
        return delta
    except (ValueError, TypeError):
        return None


def get_expiration_status(days_to_expiration: int | None) -> str:
    """
    Map days to expiration into status categories.

    Args:
        days_to_expiration: Days until expiration (can be negative)

    Returns:
        Status string: 'expired', 'critical', 'warning', 'ok', 'fresh', or 'none'
    """
    if days_to_expiration is None:
        return "none"

    if days_to_expiration < 0:
        return "expired"
    elif days_to_expiration <= 2:
        return "critical"
    elif days_to_expiration <= 6:
        return "warning"
    elif days_to_expiration <= 13:
        return "ok"
    else:
        return "fresh"


def restock_item(
    product_id: str,
    level: int = 100,
    description: str | None = None,
    user_id: str | None = None,
    quantity: float | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    """
    Set item to restocked level (default 100%) for `user_id`'s pantry.

    Called automatically when an order is placed, or manually
    when user restocks from another source.

    Args:
        product_id: The product identifier
        level: Percentage level (0-100), default 100
        description: Product description (optional)
        user_id: Owner. None resolves to the migration-installed default user.
        quantity: Absolute count restocked (e.g. 2 cans). Stored alongside %
            so partial-fulfillment math has real units to work with. None
            leaves the prior on-hand quantity untouched.
        unit: Unit label paired with quantity (e.g. "can", "lb"). Persisted
            when quantity is provided.

    Returns:
        Dict with success status and item info (includes expiration data)
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    level = max(0, min(100, level))  # Clamp to 0-100
    now = datetime.now().isoformat()

    # Calculate depletion rate from analytics
    depletion_rate = calculate_depletion_rate(product_id, user_id=owner)

    conn = get_db_connection()
    try:
        # Get description from products table if not provided (products is global)
        if not description:
            cursor = conn.execute(
                "SELECT description FROM products WHERE product_id = ?", (product_id,)
            )
            row = cursor.fetchone()
            description = row["description"] if row else None

        # Get category from product_statistics for expiration calculation
        cursor = conn.execute(
            "SELECT detected_category FROM product_statistics "
            "WHERE product_id = ? AND user_id = ?",
            (product_id, owner),
        )
        row = cursor.fetchone()
        category = row["detected_category"] if row else "regular"

        # AUTO-CALCULATE EXPIRATION DATE (no user input needed!)
        purchase_date = datetime.now().isoformat()
        expiration_date = calculate_expiration_date(purchase_date, category, description or "")
        days_to_exp = calculate_days_to_expiration(expiration_date)

        # Ensure product exists in products table (products is global catalog)
        conn.execute(
            """
            INSERT INTO products (product_id, description, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                description = COALESCE(excluded.description, products.description),
                updated_at = excluded.updated_at
        """,
            (product_id, description, now, now),
        )

        # quantity_on_hand uses COALESCE so a None call preserves prior on-hand.
        conn.execute(
            """
            INSERT INTO pantry_items
            (user_id, product_id, description, level_percent, last_restocked_at,
             last_updated_at, daily_depletion_rate, expiration_date, days_to_expiration,
             quantity_on_hand, unit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, product_id) DO UPDATE SET
                level_percent = excluded.level_percent,
                last_restocked_at = excluded.last_restocked_at,
                last_updated_at = excluded.last_updated_at,
                daily_depletion_rate = excluded.daily_depletion_rate,
                expiration_date = excluded.expiration_date,
                days_to_expiration = excluded.days_to_expiration,
                description = COALESCE(excluded.description, description),
                quantity_on_hand = COALESCE(excluded.quantity_on_hand, quantity_on_hand),
                unit = COALESCE(excluded.unit, unit)
        """,
            (
                owner,
                product_id,
                description,
                level,
                now,
                now,
                depletion_rate,
                expiration_date,
                days_to_exp,
                quantity,
                unit,
            ),
        )
        conn.commit()

        return {
            "success": True,
            "product_id": product_id,
            "description": description,
            "level_percent": level,
            "daily_depletion_rate": round(depletion_rate, 2),
            "restocked_at": now,
            "expiration_date": expiration_date,
            "days_to_expiration": days_to_exp,
            "quantity_on_hand": quantity,
            "unit": unit,
            "auto_calculated": expiration_date is not None,
        }
    finally:
        conn.close()


def update_pantry_level(product_id: str, level: int, user_id: str | None = None) -> dict[str, Any]:
    """
    Manually set pantry level for an item owned by `user_id`.

    When level is set to 0 (empty), records a depletion event that feeds
    back into consumption rate calculations for more accurate predictions.

    Args:
        product_id: The product identifier
        level: Percentage level (0-100)
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Dict with success status and updated info
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    level = max(0, min(100, level))  # Clamp to 0-100
    now = datetime.now()
    now_str = now.isoformat()

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT id, last_restocked_at, level_percent FROM pantry_items "
            "WHERE product_id = ? AND user_id = ?",
            (product_id, owner),
        )
        row = cursor.fetchone()
        if not row:
            return {
                "success": False,
                "error": f"Item '{product_id}' not in pantry. Use add_to_pantry first.",
            }

        previous_level = row["level_percent"]
        last_restocked = row["last_restocked_at"]

        # Record depletion event when item marked as empty (level <= 5%);
        # feeds back into consumption rate calculations.
        depletion_recorded = False
        if level <= 5 and previous_level > 5 and last_restocked:
            depletion_recorded = _record_depletion_event(
                product_id, last_restocked, now_str, user_id=owner
            )

        conn.execute(
            """
            UPDATE pantry_items
            SET level_percent = ?, last_updated_at = ?
            WHERE product_id = ? AND user_id = ?
        """,
            (level, now_str, product_id, owner),
        )
        conn.commit()

        result = {
            "success": True,
            "product_id": product_id,
            "level_percent": level,
            "updated_at": now_str,
        }

        if depletion_recorded:
            result["depletion_recorded"] = True
            result["message"] = "Consumption data recorded for better predictions"

        return result
    finally:
        conn.close()


def _record_depletion_event(
    product_id: str,
    last_restocked_at: str,
    depleted_at: str,
    user_id: str | None = None,
) -> bool:
    """
    Record a pantry depletion event for consumption analytics.

    This creates a purchase event record that captures the actual consumption
    time between restock and depletion, improving prediction accuracy.

    Args:
        product_id: The product identifier
        last_restocked_at: When the item was last restocked
        depleted_at: When the item was marked as depleted
        user_id: Owner of the depletion event and pantry row.

    Returns:
        True if event was recorded, False otherwise
    """
    owner = _resolve_user_id(user_id)
    try:
        conn = get_db_connection()
        try:
            # Record as a special event type that gets included in consumption calc
            conn.execute(
                """
                INSERT INTO purchase_events
                (product_id, quantity, event_type, event_date, event_timestamp, user_id)
                VALUES (?, 1, 'pantry_depleted', ?, ?, ?)
            """,
                (product_id, depleted_at[:10], depleted_at, owner),
            )
            conn.commit()

            # Trigger stats recalculation to incorporate the new data point
            from .statistics import update_product_stats

            update_product_stats(product_id)

            # Update depletion rate based on new stats
            new_rate = calculate_depletion_rate(product_id, user_id=owner)
            conn = get_db_connection()
            conn.execute(
                """
                UPDATE pantry_items
                SET daily_depletion_rate = ?
                WHERE product_id = ? AND user_id = ?
            """,
                (new_rate, product_id, owner),
            )
            conn.commit()

            return True
        finally:
            conn.close()
    except Exception:
        return False


def add_to_pantry(
    product_id: str,
    description: str | None = None,
    level: int = 100,
    low_threshold: int = 20,
    auto_deplete: bool = True,
    user_id: str | None = None,
    quantity: float | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    """
    Add an item to `user_id`'s pantry tracking.

    Args:
        product_id: The product identifier
        description: Product description
        level: Initial percentage level (0-100)
        low_threshold: Alert when level drops below this (default 20%)
        auto_deplete: Enable automatic depletion (default True)
        user_id: Owner. None resolves to the migration-installed default user.
        quantity: Absolute count on hand (e.g. 2 cans). Optional; enables
            partial-fulfillment math when set.
        unit: Unit label paired with quantity (e.g. "can", "lb").

    Returns:
        Dict with success status
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    level = max(0, min(100, level))
    now = datetime.now().isoformat()
    depletion_rate = calculate_depletion_rate(product_id, user_id=owner) if auto_deplete else 0

    conn = get_db_connection()
    try:
        # Get description from products table if not provided (products is global)
        if not description:
            cursor = conn.execute(
                "SELECT description FROM products WHERE product_id = ?", (product_id,)
            )
            row = cursor.fetchone()
            description = row["description"] if row else None

        # Ensure product exists in products table (global catalog, required for FK)
        conn.execute(
            """
            INSERT INTO products (product_id, description, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                description = COALESCE(excluded.description, products.description),
                updated_at = excluded.updated_at
        """,
            (product_id, description, now, now),
        )

        conn.execute(
            """
            INSERT INTO pantry_items
            (user_id, product_id, description, level_percent, last_restocked_at,
             last_updated_at, auto_deplete, daily_depletion_rate, low_threshold,
             quantity_on_hand, unit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, product_id) DO UPDATE SET
                description = COALESCE(excluded.description, description),
                level_percent = excluded.level_percent,
                last_restocked_at = excluded.last_restocked_at,
                last_updated_at = excluded.last_updated_at,
                daily_depletion_rate = excluded.daily_depletion_rate,
                low_threshold = excluded.low_threshold,
                auto_deplete = excluded.auto_deplete,
                quantity_on_hand = COALESCE(excluded.quantity_on_hand, quantity_on_hand),
                unit = COALESCE(excluded.unit, unit)
        """,
            (
                owner,
                product_id,
                description,
                level,
                now,
                now,
                1 if auto_deplete else 0,
                depletion_rate,
                low_threshold,
                quantity,
                unit,
            ),
        )
        conn.commit()

        return {
            "success": True,
            "product_id": product_id,
            "description": description,
            "level_percent": level,
            "low_threshold": low_threshold,
            "auto_deplete": auto_deplete,
            "quantity_on_hand": quantity,
            "unit": unit,
        }
    finally:
        conn.close()


def remove_from_pantry(product_id: str, user_id: str | None = None) -> dict[str, Any]:
    """
    Remove an item from `user_id`'s pantry tracking.

    Args:
        product_id: The product identifier
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Dict with success status
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM pantry_items WHERE product_id = ? AND user_id = ?",
            (product_id, owner),
        )
        conn.commit()

        if cursor.rowcount > 0:
            return {"success": True, "message": f"Removed '{product_id}' from pantry"}
        else:
            return {"success": False, "error": f"Item '{product_id}' not found"}
    finally:
        conn.close()


def get_pantry_status(
    apply_depletion: bool = True, user_id: str | None = None
) -> list[dict[str, Any]]:
    """
    Get all pantry items owned by `user_id` with current estimated levels
    and expiration status.

    If apply_depletion is True, calculates current level based on
    time elapsed since last update and depletion rate.

    EXPIRATION TRACKING:
    - Automatically recalculates days_to_expiration for current date
    - Includes expiration_status ('expired', 'critical', 'warning', 'ok', 'fresh', 'none')

    Args:
        apply_depletion: Whether to calculate current depleted level
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        List of pantry items with status info including expiration data
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            SELECT product_id, description, level_percent,
                   last_restocked_at, last_updated_at,
                   auto_deplete, daily_depletion_rate, low_threshold,
                   expiration_date, days_to_expiration,
                   quantity_on_hand, unit, last_used_at, last_used_source
            FROM pantry_items
            WHERE user_id = ?
            ORDER BY level_percent ASC
        """,
            (owner,),
        )

        items = []
        now = datetime.now()

        for row in cursor.fetchall():
            item = dict(row)
            level = item["level_percent"]

            # Apply depletion if enabled
            if apply_depletion and item["auto_deplete"] and item["daily_depletion_rate"]:
                last_updated = item["last_updated_at"]
                if last_updated:
                    try:
                        last_dt = datetime.fromisoformat(last_updated)
                        days_elapsed = (now - last_dt).total_seconds() / 86400
                        depletion = days_elapsed * item["daily_depletion_rate"]
                        level = max(0, level - depletion)
                    except (ValueError, TypeError):
                        pass

            # Calculate days until empty
            days_until_empty = None
            if item["daily_depletion_rate"] and item["daily_depletion_rate"] > 0:
                days_until_empty = round(level / item["daily_depletion_rate"], 1)

            # Determine inventory status
            if level <= 0:
                status = "out"
            elif level <= item["low_threshold"]:
                status = "low"
            else:
                status = "ok"

            # LAZY RECALCULATION: Always recalculate days_to_expiration for current date
            exp_date = item["expiration_date"]
            days_to_exp = calculate_days_to_expiration(exp_date)
            exp_status = get_expiration_status(days_to_exp)

            items.append(
                {
                    "product_id": item["product_id"],
                    "description": item["description"],
                    "level_percent": round(level),
                    "status": status,
                    "days_until_empty": days_until_empty,
                    "last_restocked": item["last_restocked_at"],
                    "low_threshold": item["low_threshold"],
                    "auto_deplete": bool(item["auto_deplete"]),
                    "daily_depletion_rate": (
                        round(item["daily_depletion_rate"], 2)
                        if item["daily_depletion_rate"]
                        else 0
                    ),
                    "expiration_date": exp_date,
                    "days_to_expiration": days_to_exp,
                    "expiration_status": exp_status,
                    "quantity_on_hand": item["quantity_on_hand"],
                    "unit": item["unit"],
                    "last_used_at": item["last_used_at"],
                    "last_used_source": item["last_used_source"],
                }
            )

        return items
    finally:
        conn.close()


def get_low_inventory_items(
    threshold: int | None = None, user_id: str | None = None
) -> list[dict[str, Any]]:
    """
    Get items below their low threshold for `user_id`'s pantry.

    Args:
        threshold: Override threshold (use item's own threshold if None)
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        List of low inventory items
    """
    owner = _resolve_user_id(user_id)
    items = get_pantry_status(apply_depletion=True, user_id=owner)

    low_items = []
    for item in items:
        check_threshold = threshold if threshold is not None else item["low_threshold"]
        if item["level_percent"] <= check_threshold:
            low_items.append(item)

    return low_items


def apply_daily_depletion(user_id: str | None = None) -> dict[str, Any]:
    """
    Apply depletion to all pantry items owned by `user_id` based on their rates.

    This updates the stored level_percent values in the database.
    Can be called periodically or on-demand.

    Args:
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Summary of updates applied
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        now = datetime.now()
        now_str = now.isoformat()

        cursor = conn.execute(
            """
            SELECT product_id, level_percent, last_updated_at,
                   daily_depletion_rate
            FROM pantry_items
            WHERE user_id = ? AND auto_deplete = 1 AND daily_depletion_rate > 0
        """,
            (owner,),
        )

        updated_count = 0
        for row in cursor.fetchall():
            last_updated = row["last_updated_at"]
            if not last_updated:
                continue

            try:
                last_dt = datetime.fromisoformat(last_updated)
                days_elapsed = (now - last_dt).total_seconds() / 86400

                if days_elapsed < 0.01:  # Skip if < ~15 minutes
                    continue

                depletion = days_elapsed * row["daily_depletion_rate"]
                new_level = max(0, row["level_percent"] - depletion)

                conn.execute(
                    """
                    UPDATE pantry_items
                    SET level_percent = ?, last_updated_at = ?
                    WHERE product_id = ? AND user_id = ?
                """,
                    (round(new_level), now_str, row["product_id"], owner),
                )
                updated_count += 1
            except (ValueError, TypeError):
                continue

        conn.commit()

        return {"success": True, "items_updated": updated_count, "updated_at": now_str}
    finally:
        conn.close()


def get_pantry_item(product_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """
    Get a single pantry item by product ID from `user_id`'s pantry.

    Args:
        product_id: The product identifier
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Pantry item info or None if not found
    """
    owner = _resolve_user_id(user_id)
    items = get_pantry_status(apply_depletion=True, user_id=owner)
    for item in items:
        if item["product_id"] == product_id:
            return item
    return None


def consume_from_pantry(
    product_id: str,
    quantity: float = 0,
    unit: str = "each",
    percent: float | None = None,
    source_type: str = "",
    source_id: str = "",
    source_description: str = "",
    user_id: str | None = None,
    recipe_id: str | None = None,
    event_type: str = "recipe_consumed",
) -> dict[str, Any]:
    """
    Deduct from `user_id`'s pantry inventory based on consumption.

    Writes an enriched row to purchase_events so the UI can render
    last-used attribution, the sparkline, and the detail-drawer history.
    Decrements quantity_on_hand if both `quantity` (>0) and a stored
    unit are present.

    Args:
        product_id: Product identifier
        quantity: Quantity consumed (used for unit-based decrement and the
            event_type sparkline). Caller may also pass `percent` to override
            the level math directly.
        unit: Unit of measurement (matched against stored unit before
            decrementing quantity_on_hand)
        percent: Percentage points to deduct (overrides quantity-based math)
        source_type: Logical channel of the consumption — e.g. "meal_plan",
            "gap_reconciled", "manual". Persisted for analytics.
        source_id: Free-form ID of the source (meal entry id, etc.)
        source_description: Human-readable label written to last_used_source
        user_id: Owner. None resolves to the migration-installed default user.
        recipe_id: Recipe that drove this consumption, if any
        event_type: purchase_events.event_type value. Defaults to
            "recipe_consumed"; pass "manual_use" / "gap_reconciled" as
            appropriate.

    Returns:
        Dictionary with success status and details
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT level_percent, last_restocked_at, daily_depletion_rate, "
            "       quantity_on_hand, unit "
            "FROM pantry_items WHERE product_id = ? AND user_id = ?",
            (product_id, owner),
        )
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": f"Item '{product_id}' not in pantry"}

        previous_level = row["level_percent"]
        last_restocked = row["last_restocked_at"]
        prev_quantity = row["quantity_on_hand"]
        stored_unit = row["unit"]

        if percent is not None:
            deduction = percent
        else:
            cursor = conn.execute(
                "SELECT avg_days_between_purchases FROM product_statistics "
                "WHERE product_id = ? AND user_id = ?",
                (product_id, owner),
            )
            stats_row = cursor.fetchone()
            avg_days = (stats_row["avg_days_between_purchases"] if stats_row else None) or 7
            avg_days = max(avg_days, 7)
            deduction = min(quantity * (100 / avg_days), 50)

        new_level = max(0, min(100, previous_level - deduction))

        # Only decrement on_hand when the caller's unit matches what we stored;
        # mismatched units (e.g. "tsp" vs stored "lb") would silently corrupt the count.
        new_quantity = prev_quantity
        if (
            prev_quantity is not None
            and quantity > 0
            and stored_unit
            and unit
            and stored_unit.lower() == unit.lower()
        ):
            new_quantity = max(0.0, prev_quantity - quantity)

        now_str = datetime.now().isoformat()
        last_used_source = source_description or source_type or "manual"

        conn.execute(
            "UPDATE pantry_items SET level_percent = ?, last_updated_at = ?, "
            "                        quantity_on_hand = ?, "
            "                        last_used_at = ?, last_used_source = ? "
            "WHERE product_id = ? AND user_id = ?",
            (new_level, now_str, new_quantity, now_str, last_used_source, product_id, owner),
        )

        conn.execute(
            """
            INSERT INTO purchase_events
            (product_id, quantity, event_type, event_date, event_timestamp,
             user_id, recipe_id, quantity_delta, unit, source_description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                int(quantity) if quantity else 1,
                event_type,
                now_str[:10],
                now_str,
                owner,
                recipe_id,
                -float(quantity) if quantity else -deduction,
                unit,
                last_used_source,
            ),
        )

        if new_level <= 5 and previous_level > 5 and last_restocked:
            _record_depletion_event(product_id, last_restocked, now_str, user_id=owner)

        conn.commit()

        return {
            "success": True,
            "product_id": product_id,
            "previous_level": previous_level,
            "new_level": new_level,
            "amount_deducted": deduction,
            "previous_quantity": prev_quantity,
            "new_quantity": new_quantity,
            "unit": stored_unit,
            "remaining_display": f"{new_level:.1f}%",
            "updated_at": now_str,
            "last_used_source": last_used_source,
        }

    finally:
        conn.close()


def get_usage_history(
    product_id: str,
    days: int = 30,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return recent purchase_events for one pantry product, newest first.

    Powers the sparkline and per-item detail drawer. Includes restocks
    (order_placed) and consumption (recipe_consumed, manual_use,
    gap_reconciled, pantry_depleted) so the timeline tells a complete story.
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            SELECT event_type, event_timestamp, quantity, quantity_delta,
                   unit, recipe_id, source_description, order_id
            FROM purchase_events
            WHERE product_id = ? AND user_id = ? AND event_timestamp >= ?
            ORDER BY event_timestamp DESC
            LIMIT 200
            """,
            (product_id, owner, cutoff),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


_VALID_GAP_RESOLUTIONS = ("pantry_covered", "user_skipped", "manual_acquired")


def create_pending_gap(
    product_id: str,
    needed_quantity: float,
    ordered_quantity: float,
    unit: str | None = None,
    recipe_id: str | None = None,
    recipe_name: str | None = None,
    product_description: str | None = None,
    user_id: str | None = None,
) -> int:
    """
    Record that a placed order under-fulfilled a recipe requirement.

    The shortfall stays open until the user reconciles it via resolve_gap,
    which is how partial fulfillment ("ordered 1 of the 2 cans the recipe
    needed") becomes a real pantry consumption event.
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO pending_gaps
            (user_id, recipe_id, recipe_name, product_id, product_description,
             needed_quantity, ordered_quantity, unit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner,
                recipe_id,
                recipe_name,
                product_id,
                product_description,
                needed_quantity,
                ordered_quantity,
                unit,
            ),
        )
        conn.commit()
        gap_id = cursor.lastrowid
        if gap_id is None:
            raise RuntimeError("Failed to create pending gap: no row id returned")
        return gap_id
    finally:
        conn.close()


def list_pending_gaps(user_id: str | None = None) -> list[dict[str, Any]]:
    """Return unresolved gaps for the gap-reconciliation inbox, newest first."""
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            SELECT id, recipe_id, recipe_name, product_id, product_description,
                   needed_quantity, ordered_quantity, unit, created_at
            FROM pending_gaps
            WHERE user_id = ? AND resolved_at IS NULL
            ORDER BY created_at DESC
            """,
            (owner,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def resolve_gap(
    gap_id: int,
    resolution: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Close a pending gap. If `resolution` is "pantry_covered" the shortfall
    is deducted from the pantry as a `gap_reconciled` consumption event.

    Raises ValueError on unknown resolution; caller (API/MCP) maps that
    to a 400.
    """
    if resolution not in _VALID_GAP_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {_VALID_GAP_RESOLUTIONS}, got {resolution!r}")

    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT product_id, product_description, recipe_id, recipe_name, "
            "       needed_quantity, ordered_quantity, unit, resolved_at "
            "FROM pending_gaps WHERE id = ? AND user_id = ?",
            (gap_id, owner),
        )
        gap = cursor.fetchone()
        if not gap:
            return {"success": False, "error": f"Gap {gap_id} not found"}
        if gap["resolved_at"]:
            return {"success": False, "error": f"Gap {gap_id} already resolved"}

        shortfall = max(0.0, gap["needed_quantity"] - gap["ordered_quantity"])
        consumed_result: dict[str, Any] | None = None

        if resolution == "pantry_covered" and shortfall > 0:
            consumed_result = consume_from_pantry(
                product_id=gap["product_id"],
                quantity=shortfall,
                unit=gap["unit"] or "each",
                source_type="gap_reconciled",
                source_id=str(gap_id),
                source_description=(
                    f"Gap covered: {gap['recipe_name'] or 'recipe'} "
                    f"needed {gap['needed_quantity']}, ordered {gap['ordered_quantity']}"
                ),
                user_id=owner,
                recipe_id=gap["recipe_id"],
                event_type="gap_reconciled",
            )

        now_str = datetime.now().isoformat()
        conn.execute(
            "UPDATE pending_gaps SET resolved_at = ?, resolution = ? WHERE id = ?",
            (now_str, resolution, gap_id),
        )
        conn.commit()

        return {
            "success": True,
            "gap_id": gap_id,
            "resolution": resolution,
            "shortfall": shortfall,
            "consumed": consumed_result,
        }
    finally:
        conn.close()
