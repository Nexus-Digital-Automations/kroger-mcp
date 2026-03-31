"""
Meal tracker — log meals/snacks and deduct from pantry in real time.

Tracks individual consumption events with pantry integration:
- Log meals with pantry items and percentage consumed
- Automatic pantry deduction on log
- Exact undo on delete (previous_level stored per item)
- Daily summaries and history queries
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from .database import get_db_connection, ensure_initialized
from .pantry import consume_from_pantry, get_pantry_item, get_pantry_status, update_pantry_level


VALID_MEAL_TYPES = {'breakfast', 'lunch', 'dinner', 'snack'}


def log_meal(
    meal_type: str,
    items: List[Dict[str, Any]],
    description: Optional[str] = None,
    recipe_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Log a meal and deduct from pantry.

    Args:
        meal_type: One of breakfast, lunch, dinner, snack
        items: List of dicts with product_id, description (optional),
               quantity_percent (percentage points to deduct, 0-100)
        description: Optional meal description
        recipe_id: Optional linked recipe
        notes: Optional notes

    Returns:
        Dict with success, log_id, pantry_updates
    """
    ensure_initialized()

    if meal_type not in VALID_MEAL_TYPES:
        return {
            'success': False,
            'error': f"Invalid meal_type '{meal_type}'. Must be one of: {', '.join(sorted(VALID_MEAL_TYPES))}",
        }

    if not items:
        return {'success': False, 'error': 'At least one item is required'}

    now = datetime.now().isoformat()
    pantry_updates = []
    errors = []

    conn = get_db_connection()
    try:
        # Insert meal log entry
        cursor = conn.execute(
            """INSERT INTO meal_log (logged_at, meal_type, description, recipe_id, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (now, meal_type, description, recipe_id, notes),
        )
        log_id = cursor.lastrowid

        for item in items:
            product_id = item.get('product_id')
            if not product_id:
                continue

            item_desc = item.get('description', '')
            qty_pct = float(item.get('quantity_percent', 10))
            qty_pct = max(0, min(100, qty_pct))

            # Get current pantry level before deduction
            pantry_item = get_pantry_item(product_id)
            previous_level = pantry_item['level_percent'] if pantry_item else None

            # Deduct from pantry
            deducted = False
            if pantry_item and qty_pct > 0:
                result = consume_from_pantry(product_id=product_id, percent=qty_pct)
                if result.get('success'):
                    deducted = True
                    pantry_updates.append({
                        'product_id': product_id,
                        'description': item_desc or pantry_item.get('description', ''),
                        'previous_level': result['previous_level'],
                        'new_level': result['new_level'],
                        'amount_deducted': result['amount_deducted'],
                    })
                else:
                    errors.append({
                        'product_id': product_id,
                        'error': result.get('error', 'Unknown error'),
                    })

            # Insert meal log item
            conn.execute(
                """INSERT INTO meal_log_items
                   (meal_log_id, product_id, description, quantity_percent, previous_level, pantry_deducted)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (log_id, product_id, item_desc, qty_pct, previous_level, 1 if deducted else 0),
            )

        conn.commit()

        return {
            'success': True,
            'log_id': log_id,
            'meal_type': meal_type,
            'logged_at': now,
            'items_logged': len(items),
            'pantry_updates': pantry_updates,
            'errors': errors,
        }

    except Exception as e:
        conn.rollback()
        return {'success': False, 'error': f'Failed to log meal: {str(e)}'}
    finally:
        conn.close()


def get_meal_log(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieve meal log entries with items, filtered by date range.

    Args:
        date_from: ISO date string (YYYY-MM-DD), inclusive
        date_to: ISO date string (YYYY-MM-DD), inclusive

    Returns:
        Dict with success and entries list
    """
    ensure_initialized()

    conn = get_db_connection()
    try:
        query = "SELECT id, logged_at, meal_type, description, recipe_id, notes FROM meal_log"
        params = []
        conditions = []

        if date_from:
            conditions.append("logged_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("logged_at < ?")
            # Include the full day
            params.append(date_to + "T23:59:59.999999")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY logged_at DESC"

        cursor = conn.execute(query, params)
        logs = [dict(r) for r in cursor.fetchall()]

        # Fetch items for each log
        for log in logs:
            items_cursor = conn.execute(
                """SELECT product_id, description, quantity_percent, previous_level, pantry_deducted
                   FROM meal_log_items WHERE meal_log_id = ?""",
                (log['id'],),
            )
            log['items'] = [dict(r) for r in items_cursor.fetchall()]

        return {'success': True, 'entries': logs}

    except Exception as e:
        return {'success': False, 'error': str(e), 'entries': []}
    finally:
        conn.close()


def get_today_meals() -> Dict[str, Any]:
    """
    Get today's meal log with summary statistics.

    Returns:
        Dict with meals list and stats
    """
    today = datetime.now().strftime('%Y-%m-%d')
    result = get_meal_log(date_from=today, date_to=today)

    meals = result.get('entries', [])
    items_consumed = sum(len(m.get('items', [])) for m in meals)

    # Count items currently low in pantry
    pantry_items = get_pantry_status(apply_depletion=True)
    low_items = sum(1 for i in pantry_items if i.get('status') == 'low' or i.get('status') == 'out')

    return {
        'success': True,
        'date': today,
        'meals': meals,
        'stats': {
            'meals_count': len(meals),
            'items_consumed': items_consumed,
            'low_items': low_items,
        },
    }


def delete_meal_log(log_id: int) -> Dict[str, Any]:
    """
    Delete a meal log entry and restore pantry levels.

    Args:
        log_id: The meal log ID to delete

    Returns:
        Dict with success and restored items
    """
    ensure_initialized()

    conn = get_db_connection()
    try:
        # Check if the log exists
        cursor = conn.execute("SELECT id FROM meal_log WHERE id = ?", (log_id,))
        if not cursor.fetchone():
            return {'success': False, 'error': f'Meal log {log_id} not found'}

        # Get items to restore
        items_cursor = conn.execute(
            """SELECT product_id, quantity_percent, previous_level, pantry_deducted
               FROM meal_log_items WHERE meal_log_id = ?""",
            (log_id,),
        )
        items = [dict(r) for r in items_cursor.fetchall()]

        restored = []
        for item in items:
            if item['pantry_deducted'] and item['previous_level'] is not None:
                # Restore to previous level
                result = update_pantry_level(item['product_id'], int(item['previous_level']))
                if result.get('success'):
                    restored.append({
                        'product_id': item['product_id'],
                        'restored_to': item['previous_level'],
                    })

        # Delete the log entry (CASCADE deletes items)
        conn.execute("DELETE FROM meal_log WHERE id = ?", (log_id,))
        conn.commit()

        return {
            'success': True,
            'deleted_log_id': log_id,
            'restored_items': restored,
        }

    except Exception as e:
        conn.rollback()
        return {'success': False, 'error': f'Failed to delete meal log: {str(e)}'}
    finally:
        conn.close()
