"""FastAPI dependencies for resolving the authenticated user.

Owns: the contract between AuthMiddleware (which sets request.state.user) and
route handlers / analytics functions (which need a user_id to scope queries).

Resolvers:
    - current_user() / current_user_id(): for HTTP route handlers — pulls the
      user attached by AuthMiddleware. Raises 401 if missing.
    - mcp_user_id(): for MCP tool dispatchers — reads KROGER_MCP_USER_ID
      per-invocation (lets each Claude Desktop profile bind to a specific
      user), falls back to KROGER_MCP_DEFAULT_USER_ID for backward compat.
    - default_user_id(): bare-bones fallback for scripts and tests, reads
      only KROGER_MCP_DEFAULT_USER_ID. Raises if unset.

@stable
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, Request


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


def mcp_user_id() -> str:
    """For MCP tool dispatchers — resolves the user the MCP invocation acts as.

    Resolution order:
      1. KROGER_MCP_USER_ID — set per Claude Desktop config so each profile
         can bind to a specific user (`"env": {"KROGER_MCP_USER_ID": "<uuid>"}`).
      2. KROGER_MCP_DEFAULT_USER_ID — the migration-installed owner, used when
         no per-profile override is configured.

    Raises RuntimeError if neither is set — surfaces misconfiguration loudly.
    """
    explicit = os.environ.get("KROGER_MCP_USER_ID")
    if explicit:
        return explicit
    return default_user_id()
