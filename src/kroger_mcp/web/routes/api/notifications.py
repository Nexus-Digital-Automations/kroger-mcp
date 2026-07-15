"""Notification API — favorite-on-sale alerts for the header bell.

Read + state endpoints over ``analytics.notifications``; all user-scoped via the
session cookie. Detection itself runs in the background scanner, not here.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics import notifications
from kroger_mcp.analytics.database import run_in_thread
from kroger_mcp.auth.dependencies import current_user_id

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/notifications")
async def get_notifications(request: Request):
    """Active favorite-on-sale alerts + unseen count for the bell badge."""
    try:
        user_id = current_user_id(request)
        alerts = await run_in_thread(notifications.list_alerts, user_id)
        unseen = await run_in_thread(notifications.unseen_count, user_id)
        pending_meals = await run_in_thread(notifications.list_pending_meals_for_bell, user_id)
        unseen += len(pending_meals)
        return JSONResponse(
            content={"alerts": alerts, "pending_meals": pending_meals, "unseen": unseen}
        )
    except Exception as exc:
        logger.exception("get_notifications failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/notifications/mark-seen")
async def mark_seen(request: Request):
    """Clear the badge once the popup is opened."""
    try:
        user_id = current_user_id(request)
        seen = await run_in_thread(notifications.mark_alerts_seen, user_id)
        return JSONResponse(content={"success": True, "seen": seen})
    except Exception as exc:
        logger.exception("mark_seen failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


class DismissBody(BaseModel):
    acted: bool = False


@router.post("/api/notifications/{alert_id}/dismiss")
async def dismiss(alert_id: int, request: Request, body: DismissBody | None = None):
    """Remove one alert. ``acted=true`` also records that the user added it."""
    try:
        user_id = current_user_id(request)
        if body is not None and body.acted:
            ok = await run_in_thread(notifications.mark_acted, user_id, alert_id)
        else:
            ok = await run_in_thread(notifications.dismiss_alert, user_id, alert_id)
        if not ok:
            return JSONResponse(status_code=404, content={"error": "Alert not found"})
        return JSONResponse(content={"success": True})
    except Exception as exc:
        logger.exception("dismiss alert failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})
