"""Snacks route — dedicated management view for the built-in Snacks list.

There is exactly one Snacks list per user (list_type='snacks'), so /snacks goes
straight to the detail-equivalent view: the replenishment check-up merged with
the list's item metadata. Browse/act reuse the existing favorites API endpoints.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kroger_mcp.analytics.database import run_in_thread
from kroger_mcp.analytics.favorites import (
    _ensure_snacks_list_for_user,
    check_snacks,
    get_list_items,
)
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.web.templating import templates

router = APIRouter()


def _snacks_payload(user_id: str) -> dict:
    """Blocking work for the snacks page (DB reads), run off the event loop.

    Merges ``check_snacks`` (heuristic fields: reason, pre_ticked,
    days_since_ordered, typical_gap_days, never_ordered) with ``get_list_items``
    (times_ordered, notes, pantry_status) by product_id.
    """
    list_id = _ensure_snacks_list_for_user(user_id)
    check = check_snacks(user_id=user_id)
    candidates = check.get("candidates", [])

    items_result = get_list_items(list_id, include_pantry_status=True, user_id=user_id)
    by_pid = {it["product_id"]: it for it in items_result.get("items", [])}

    items = []
    for cand in candidates:
        meta = by_pid.get(cand["product_id"], {})
        ps = meta.get("pantry_status") or {}
        level = cand.get("pantry_level")
        if level is None:
            level = ps.get("level_percent")

        if level is None:
            level_status = "unknown"
        elif level <= 0:
            level_status = "out"
        elif ps.get("is_low"):
            level_status = "low"
        else:
            level_status = "ok"

        items.append(
            {
                "product_id": cand["product_id"],
                "list_id": cand["list_id"],
                "description": cand.get("description"),
                "brand": cand.get("brand"),
                "level_percent": level,
                "level_status": level_status,
                "reason": cand.get("reason"),
                "pre_ticked": cand.get("pre_ticked", False),
                "days_since_ordered": cand.get("days_since_ordered"),
                "never_ordered": cand.get("never_ordered", False),
                "default_quantity": cand.get("default_quantity") or meta.get("default_quantity") or 1,
                "typical_gap_days": cand.get("typical_gap_days"),
                "times_ordered": meta.get("times_ordered") or 0,
                "notes": meta.get("notes"),
            }
        )

    return {
        "active_page": "snacks",
        "list_id": list_id,
        "items": items,
        "flagged_count": check.get("flagged_count", 0),
        "ticked_map": {it["product_id"]: it["pre_ticked"] for it in items},
    }


@router.get("/snacks", response_class=HTMLResponse)
async def snacks_page(request: Request):
    context = await run_in_thread(_snacks_payload, current_user_id(request))
    return templates.TemplateResponse(request, "snacks.html", context)
