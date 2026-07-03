"""Per-user ingredient filtering preferences."""

from typing import Any

from ..database import ensure_initialized, get_db_cursor
from ._common import _resolve_user_id


def get_disabled_ingredients(user_id: str | None = None) -> set[str]:
    """Get set of ingredient keys that this user has disabled."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT ingredient_key FROM ingredient_preferences WHERE user_id = ? AND enabled = 0",
            (resolved,),
        )
        return {row["ingredient_key"] for row in cursor.fetchall()}


def toggle_ingredient(
    ingredient_key: str, enabled: bool, user_id: str | None = None
) -> dict[str, Any]:
    """Enable or disable checking for a specific ingredient for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)
    flag = 1 if enabled else 0

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ingredient_preferences (user_id, ingredient_key, enabled, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, ingredient_key) DO UPDATE SET
                enabled = ?,
                updated_at = CURRENT_TIMESTAMP
            """,
            (resolved, ingredient_key, flag, flag),
        )

    return {
        "success": True,
        "ingredient_key": ingredient_key,
        "enabled": enabled,
    }


def get_ingredient_preferences(user_id: str | None = None) -> list[dict[str, Any]]:
    """Get all ingredient preferences for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT ingredient_key, enabled, severity, updated_at
            FROM ingredient_preferences
            WHERE user_id = ?
            ORDER BY ingredient_key
            """,
            (resolved,),
        )
        return [dict(row) for row in cursor.fetchall()]


def reset_ingredient_preferences(user_id: str | None = None) -> dict[str, Any]:
    """Reset all ingredient preferences to defaults (all enabled) for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM ingredient_preferences WHERE user_id = ?", (resolved,))
        deleted = cursor.rowcount

    return {"success": True, "message": f"Reset {deleted} ingredient preferences to defaults"}
