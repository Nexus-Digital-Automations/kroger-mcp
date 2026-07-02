"""
Shared user-scoping helper for analytics modules.
"""

from kroger_mcp.auth.dependencies import mcp_user_id


def resolve_user_id(user_id: str | None) -> str:
    """Resolve user_id for user-scoped queries.

    HTTP route handlers always pass user_id from the session. MCP/script
    callers may pass None; we fall back to `mcp_user_id()` which honors
    KROGER_MCP_USER_ID per Claude Desktop profile, then
    KROGER_MCP_DEFAULT_USER_ID. This means MCP profiles bound to different
    users see only their own data — no per-tool-dispatcher threading needed.
    """
    return user_id if user_id is not None else mcp_user_id()
