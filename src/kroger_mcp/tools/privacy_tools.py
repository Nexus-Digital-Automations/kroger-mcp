"""
Privacy / data-sharing consent tools for the Kroger MCP server.

Exposes the consent domain (analytics/consent.py) over MCP so the assistant can
show the user what they share, change it, withdraw it, or delete shared data —
always opt-in, always per-user.
"""

import asyncio
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from ..analytics import consent as _consent
from ..auth.dependencies import mcp_user_id


def register_tools(mcp):
    """Register the privacy/consent tool with the FastMCP server."""

    @mcp.tool()
    async def privacy(
        action: Literal[
            "get_consent",
            "set_consent",
            "withdraw",
            "delete_my_data",
        ] = Field(description="get_consent|set_consent|withdraw|delete_my_data"),
        updates: dict[str, bool] | None = Field(
            default=None,
            description=(
                "For set_consent: map of category key → opt-in boolean. Valid keys: "
                "purchase_patterns, price_observations, consumption, recipe_trends."
            ),
        ),
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Manage anonymized data-sharing consent (opt-in, off by default).

        Sharing only ever covers de-identified aggregate trends — never names,
        accounts, or individual baskets — and declining never degrades the app.
        """
        return await asyncio.to_thread(_privacy_impl, action, updates, ctx)


def _privacy_impl(action: str, updates: dict[str, bool] | None, ctx) -> dict[str, Any]:
    # ctx is left untyped to match the other tool impls: it is the FastMCP
    # Context whose info() is fire-and-forget logging from this threaded handler.
    user_id = mcp_user_id()

    match action:
        case "get_consent":
            if ctx:
                ctx.info("Getting data-sharing consent")
            return {"success": True, **_consent.get_consent(user_id=user_id)}

        case "set_consent":
            if ctx:
                ctx.info("Updating data-sharing consent")
            if not updates:
                return {
                    "success": False,
                    "error": "set_consent requires 'updates' (category → boolean).",
                }
            try:
                return {"success": True, **_consent.set_consent(updates, user_id=user_id)}
            except KeyError as exc:
                return {"success": False, "error": str(exc)}

        case "withdraw":
            if ctx:
                ctx.info("Withdrawing all data-sharing consent")
            return {"success": True, **_consent.withdraw_consent(user_id=user_id)}

        case "delete_my_data":
            if ctx:
                ctx.info("Deleting shared data and withdrawing consent")
            return {"success": True, **_consent.delete_shared_data(user_id=user_id)}

        case _:
            return {"success": False, "error": f"Unknown action: {action}"}
