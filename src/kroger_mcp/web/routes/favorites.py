"""Favorites routes — list overview and detail view."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.favorites import get_list_items, get_lists
from kroger_mcp.web.context import action_menu_context

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


def _reorder_badge(reorder_status):
    """Return (label, css_color_class) for the reorder status."""
    if not reorder_status.get("has_schedule"):
        return "No Schedule", "gray"
    status = reorder_status.get("status", "")
    if status == "overdue":
        return "Overdue", "red"
    elif status == "due_soon":
        return "Due Soon", "amber"
    elif status == "never_ordered":
        return "Never Ordered", "red"
    else:
        return "On Schedule", "emerald"


@router.get("/favorites", response_class=HTMLResponse)
async def favorites_list(request: Request):
    lists = get_lists()

    annotated = []
    for lst in lists:
        label, color = _reorder_badge(lst.get("reorder_status", {}))
        annotated.append(
            {
                **lst,
                "badge_label": label,
                "badge_color": color,
            }
        )

    return templates.TemplateResponse(
        "favorites.html",
        {
            "request": request,
            "active_page": "favorites",
            "lists": annotated,
        },
    )


@router.get("/favorites/{list_id}", response_class=HTMLResponse)
async def favorites_detail(request: Request, list_id: str):
    result = get_list_items(list_id, include_pantry_status=True)

    if not result.get("success", True) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    lst = result.get("list", {})
    items = result.get("items", [])

    # Annotate items with level status (pantry data is nested in pantry_status)
    for item in items:
        ps = item.get("pantry_status") or {}
        level = ps.get("level_percent")
        item["level_percent"] = level
        if level is None:
            item["level_status"] = "unknown"
        elif level <= 0:
            item["level_status"] = "out"
        elif ps.get("is_low", False):
            item["level_status"] = "low"
        else:
            item["level_status"] = "ok"

    reorder_status = lst.get("reorder_status", {})
    badge_label, badge_color = _reorder_badge(reorder_status)

    return templates.TemplateResponse(
        "favorites_detail.html",
        {
            "request": request,
            "active_page": "favorites",
            "lst": lst,
            "items": items,
            "badge_label": badge_label,
            "badge_color": badge_color,
            "reorder_status": reorder_status,
            **action_menu_context(),
        },
    )
