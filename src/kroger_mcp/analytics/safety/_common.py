"""Shared helpers for user resolution and per-user default seeding."""

from datetime import datetime

from kroger_mcp.auth.dependencies import mcp_user_id

from ..database import get_db_cursor


def _resolve_user_id(user_id: str | None) -> str:
    """Resolve user_id for user-scoped queries.

    HTTP route handlers always pass user_id from the session. MCP/script
    callers may pass None; we fall back to `mcp_user_id()` which honors
    KROGER_MCP_USER_ID per Claude Desktop profile, then
    KROGER_MCP_DEFAULT_USER_ID. This means MCP profiles bound to different
    users see only their own data — no per-tool-dispatcher threading needed.
    """
    return user_id if user_id is not None else mcp_user_id()


def _ensure_default_safety_settings_for_user(user_id: str) -> None:
    """Lazily create default safety_settings rows for a user that has none.

    Mirrors `_ensure_default_list_for_user` in favorites.py — new users
    that have never touched safety yet get the same defaults the migration
    backfilled for the seed owner (filtering_enabled=1, block_mode='soft').
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM safety_settings WHERE user_id = ?",
            (user_id,),
        )
        if cursor.fetchone()["cnt"] > 0:
            return
        now = datetime.now().isoformat()
        cursor.execute(
            """
            INSERT INTO safety_settings (user_id, key, value, updated_at)
            VALUES (?, 'filtering_enabled', '1', ?)
            """,
            (user_id, now),
        )
        cursor.execute(
            """
            INSERT INTO safety_settings (user_id, key, value, updated_at)
            VALUES (?, 'block_mode', 'soft', ?)
            """,
            (user_id, now),
        )
