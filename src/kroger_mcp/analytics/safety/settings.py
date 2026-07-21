"""Per-user safety filter settings (filtering toggle + block mode)."""

from datetime import datetime
from typing import Any

from ..database import ensure_initialized, get_db_cursor
from ._common import _ensure_default_safety_settings_for_user, _resolve_user_id
from .models import BlockMode


def get_safety_settings(user_id: str) -> dict[str, Any]:
    """Get current safety filter settings for a user.

    First-read for a user with no rows seeds per-user defaults so subsequent
    callers see the same baseline the migration backfilled for the seed owner.
    """
    ensure_initialized()
    resolved = _resolve_user_id(user_id)
    _ensure_default_safety_settings_for_user(resolved)

    with get_db_cursor() as cursor:
        cursor.execute("SELECT key, value FROM safety_settings WHERE user_id = ?", (resolved,))
        rows = cursor.fetchall()

    settings = {
        "filtering_enabled": True,
        "block_mode": BlockMode.SOFT.value,
    }

    for row in rows:
        key = row["key"]
        value = row["value"]
        if key == "filtering_enabled":
            settings["filtering_enabled"] = value == "1"
        elif key == "block_mode":
            settings["block_mode"] = value

    return settings


def update_safety_settings(
    filtering_enabled: bool | None = None,
    block_mode: str | None = None,
    *, user_id: str,
) -> dict[str, Any]:
    """Update safety filter settings for a user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        now = datetime.now().isoformat()

        if filtering_enabled is not None:
            value = "1" if filtering_enabled else "0"
            cursor.execute(
                """
                INSERT INTO safety_settings (user_id, key, value, updated_at)
                VALUES (?, 'filtering_enabled', ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value = ?, updated_at = ?
                """,
                (resolved, value, now, value, now),
            )

        if block_mode is not None:
            if block_mode not in [m.value for m in BlockMode]:
                raise ValueError(f"Invalid block_mode: {block_mode}")
            cursor.execute(
                """
                INSERT INTO safety_settings (user_id, key, value, updated_at)
                VALUES (?, 'block_mode', ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value = ?, updated_at = ?
                """,
                (resolved, block_mode, now, block_mode, now),
            )

    return get_safety_settings(user_id=resolved)


def is_filtering_enabled(user_id: str) -> bool:
    """Check if ingredient filtering is enabled for this user."""
    settings = get_safety_settings(user_id=user_id)
    return settings.get("filtering_enabled", True)


def get_block_mode(user_id: str) -> BlockMode:
    """Get the current block mode for this user."""
    settings = get_safety_settings(user_id=user_id)
    mode_str = settings.get("block_mode", "soft")
    return BlockMode(mode_str)
