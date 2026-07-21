"""Favorites routes — list overview and detail view."""


from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from kroger_mcp.analytics.database import run_in_thread
from kroger_mcp.analytics.favorites import get_list_items, get_lists
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.web.context import action_menu_context
from kroger_mcp.web.templating import templates

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


def _favorites_payload(user_id: str) -> dict:
    """Blocking work for the favorites overview (DB reads), run off the event
    loop via run_in_thread."""
    lists = get_lists(user_id=user_id)

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

    return {
        "active_page": "favorites",
        "lists": annotated,
    }


@router.get("/favorites", response_class=HTMLResponse)
async def favorites_list(request: Request):
    context = await run_in_thread(_favorites_payload, current_user_id(request))
    return templates.TemplateResponse(request, "favorites.html", context)


def _favorites_detail_payload(list_id: str, user_id: str) -> dict:
    """Blocking work for the favorites detail page (DB reads + pantry status),
    run off the event loop via run_in_thread."""
    result = get_list_items(list_id, include_pantry_status=True, user_id=user_id)

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

    return {
        "active_page": "favorites",
        "lst": lst,
        "items": items,
        "badge_label": badge_label,
        "badge_color": badge_color,
        "reorder_status": reorder_status,
        **action_menu_context(user_id),
    }


@router.get("/favorites/{list_id}", response_class=HTMLResponse)
async def favorites_detail(request: Request, list_id: str):
    context = await run_in_thread(
        _favorites_detail_payload, list_id, current_user_id(request)
    )
    return templates.TemplateResponse(request, "favorites_detail.html", context)
