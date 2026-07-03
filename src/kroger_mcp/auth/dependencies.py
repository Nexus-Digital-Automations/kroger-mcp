"""FastAPI dependencies for resolving the authenticated user.

Owns: the contract between AuthMiddleware (which sets request.state.user) and
route handlers / analytics functions (which need a user_id to scope queries).

Resolvers:
    - current_user() / current_user_id(): for HTTP route handlers — pulls the
      user attached by AuthMiddleware. Raises 401 if missing.
    - mcp_user_id(): for MCP tool dispatchers — checks the web-request
      ContextVar first (set by chatbot routes so in-process tool calls run
      as the logged-in web user), then KROGER_MCP_USER_ID per-invocation
      (lets each Claude Desktop profile bind to a specific user), then falls
      back to KROGER_MCP_DEFAULT_USER_ID for backward compat.
    - set_web_user_id() / reset_web_user_id(): bracket a chatbot-triggered
      tool call with the requesting web user's id, so mcp_user_id() resolves
      to them instead of the MCP env-var default. Safe across
      asyncio.to_thread (context vars propagate into the worker thread).
    - default_user_id(): bare-bones fallback for scripts and tests, reads
      only KROGER_MCP_DEFAULT_USER_ID. Raises if unset.

@stable
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any

from fastapi import HTTPException, Request

_web_user_id: ContextVar[str | None] = ContextVar("_web_user_id", default=None)


def current_user(request: Request) -> dict[str, Any]:
    """Return the dict set by AuthMiddleware. Raises 401 if missing."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def current_user_id(request: Request) -> str:
    """Convenience wrapper returning just user_id."""
    return str(current_user(request)["id"])


def default_user_id() -> str:
    """For scripts / tests with no HTTP context and no per-invocation env.

    Reads KROGER_MCP_DEFAULT_USER_ID (installed by the multi-tenant migration).
    Raises RuntimeError if unset.
    """
    user_id = os.environ.get("KROGER_MCP_DEFAULT_USER_ID")
    if not user_id:
        raise RuntimeError(
            "KROGER_MCP_DEFAULT_USER_ID is unset. Run "
            "`uv run python -m kroger_mcp.scripts.migrate_to_multi_tenant` first."
        )
    return user_id


def set_web_user_id(user_id: str | None) -> Token:
    """Bind the current async context to a web-request user_id.

    Call at the start of a chatbot-triggered tool invocation; always pair
    with reset_web_user_id(token) in a `finally` block.
    """
    return _web_user_id.set(user_id)


def reset_web_user_id(token: Token) -> None:
    _web_user_id.reset(token)


def mcp_user_id() -> str:
    """For MCP tool dispatchers — resolves the user the MCP invocation acts as.

    Resolution order:
      1. The web-request ContextVar — set by chatbot routes so an in-process
         tool call triggered by the logged-in web user runs as that user,
         not the single-user MCP default.
      2. KROGER_MCP_USER_ID — set per Claude Desktop config so each profile
         can bind to a specific user (`"env": {"KROGER_MCP_USER_ID": "<uuid>"}`).
      3. KROGER_MCP_DEFAULT_USER_ID — the migration-installed owner, used when
         neither of the above is set.

    Raises RuntimeError if none of the above resolve — surfaces
    misconfiguration loudly.
    """
    web_scoped = _web_user_id.get()
    if web_scoped:
        return web_scoped
    explicit = os.environ.get("KROGER_MCP_USER_ID")
    if explicit:
        return explicit
    return default_user_id()
