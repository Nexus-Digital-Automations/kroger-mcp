"""Notifications MCP tool — the web app's header bell.

Same data and state changes as `web/routes/api/notifications.py`: favorite-on-sale
alerts, meals awaiting cook confirmation, pantry alerts, unmatched snacks, and
the "next week needs a plan" nudge. Every call is scoped to the MCP user.
"""

import asyncio
from typing import Any, Literal

from pydantic import Field

from ..analytics import notifications
from ..auth.dependencies import mcp_user_id


def register_tools(mcp):
    """Register the notifications tool with the FastMCP server."""

    # Public name "notifications"; the Python name avoids shadowing the
    # analytics module imported above.
    @mcp.tool(name="notifications")
    async def notifications_tool(
        action: Literal["list", "mark_seen", "dismiss"] = Field(
            description=(
                "list — everything the app's notification bell shows "
                "(sale alerts, pending meals, pantry alerts, unmatched snacks, "
                "needs_plan) plus the unseen count. "
                "mark_seen — clear the unseen badge for sale alerts. "
                "dismiss — remove one sale alert (alert_id); acted=True records "
                "that you acted on it (e.g. added it to the list)."
            )
        ),
        alert_id: int | None = Field(default=None, description="dismiss — alert ID from list"),
        acted: bool | None = Field(
            default=None, description="dismiss — True if the alert was acted on"
        ),
    ) -> dict[str, Any]:
        """Notification bell: list, mark seen, dismiss."""
        user_id = mcp_user_id()
        return await asyncio.to_thread(_notifications_impl, action, alert_id, acted, user_id)


def _notifications_impl(
    action: str, alert_id: int | None, acted: bool | None, user_id: str
) -> dict[str, Any]:
    match action:
        case "list":
            return {"success": True, **notifications.bell_summary(user_id)}
        case "mark_seen":
            return {"success": True, "seen": notifications.mark_alerts_seen(user_id)}
        case "dismiss":
            if alert_id is None:
                return {"success": False, "error": "alert_id is required for dismiss"}
            if acted:
                ok = notifications.mark_acted(user_id, alert_id)
            else:
                ok = notifications.dismiss_alert(user_id, alert_id)
            if not ok:
                return {"success": False, "error": f"Alert {alert_id} not found"}
            return {"success": True, "alert_id": alert_id, "acted": bool(acted)}
    return {"success": False, "error": f"Unknown action: {action}"}
