"""Pantry route — inventory overview sorted by level."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.pantry import get_pantry_status, list_pending_gaps
from kroger_mcp.auth.dependencies import current_user_id

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


@router.get("/pantry", response_class=HTMLResponse)
async def pantry_page(request: Request):
    user_id = current_user_id(request)
    items = get_pantry_status(apply_depletion=True, user_id=user_id)
    gaps = list_pending_gaps(user_id=user_id)

    out_items = [i for i in items if i["status"] == "out"]
    low_items = [i for i in items if i["status"] == "low"]
    ok_items = [i for i in items if i["status"] == "ok"]

    expiring_soon = [
        i for i in items if i.get("days_to_expiration") is not None and i["days_to_expiration"] <= 7
    ]

    return templates.TemplateResponse(request, "pantry.html",
        {
            "active_page": "pantry",
            "all_items": items,
            "out_items": out_items,
            "low_items": low_items,
            "ok_items": ok_items,
            "pending_gaps": gaps,
            "expiring_soon_count": len(expiring_soon),
            "total_count": len(items),
            "low_count": len(low_items),
            "out_count": len(out_items),
        },
    )
