"""FastAPI dependencies for resolving the authenticated user.

Owns: the contract between AuthMiddleware (which sets request.state.user) and
route handlers / analytics functions (which need a user_id to scope queries).

Two resolvers:
    - current_user(): FastAPI Depends — for HTTP route handlers, returns the user
      attached by AuthMiddleware. Raises 401 if missing.
    - default_user_id(): for non-HTTP contexts (MCP tools, scripts, background
      jobs) — reads KROGER_MCP_DEFAULT_USER_ID from env, raises if unset.

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
    """For MCP / scripts / background jobs with no HTTP context.

    Resolves to KROGER_MCP_DEFAULT_USER_ID set by the multi-tenant migration.
    Raises RuntimeError if the env var is unset — the caller MUST migrate first.
    """
    user_id = os.environ.get("KROGER_MCP_DEFAULT_USER_ID")
    if not user_id:
        raise RuntimeError(
            "KROGER_MCP_DEFAULT_USER_ID is unset. Run "
            "`uv run python -m kroger_mcp.scripts.migrate_to_multi_tenant` first."
        )
    return user_id
