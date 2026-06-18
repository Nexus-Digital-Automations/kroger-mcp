"""Guide routes — list, detail, and edit views for technique how-tos."""

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from kroger_mcp.analytics.database import run_in_thread
from kroger_mcp.tools.guide_tools import _find_guide, _load_guides
from kroger_mcp.web.context import action_menu_context
from kroger_mcp.web.templating import templates

router = APIRouter()


def _normalize_tags(guide: dict) -> None:
    """Coerce a guide's tags into a clean list (in place)."""
    if isinstance(guide.get("tags"), str):
        guide["tags"] = [t.strip() for t in guide["tags"].split(",") if t.strip()]
    elif not guide.get("tags"):
        guide["tags"] = []


def _collect_all_tags(guides: list[dict]) -> list[str]:
    """Return sorted unique tags across all guides."""
    tags = set()
    for g in guides:
        for t in g.get("tags", []):
            if isinstance(t, str) and t.strip():
                tags.add(t.strip())
    return sorted(tags)


def _guides_payload() -> dict:
    """Blocking work for the guides list (JSON load only) — run off the loop."""
    data = _load_guides()
    guides = data.get("guides", [])

    for g in guides:
        _normalize_tags(g)

    all_tags = _collect_all_tags(guides)

    guides_json = json.dumps(
        [
            {
                "id": g.get("id", ""),
                "name": g.get("name", ""),
                "description": g.get("description") or "",
                "tags": g.get("tags", []),
                "step_count": len(g.get("steps", [])),
                "time": g.get("time") or "",
                "difficulty": g.get("difficulty") or "",
            }
            for g in guides
        ]
    )

    return {
        "active_page": "guides",
        "guides": guides,
        "all_tags": all_tags,
        "guide_count": len(guides),
        "guides_json": guides_json,
        **action_menu_context(),
    }


@router.get("/guides", response_class=HTMLResponse)
async def guides_list(request: Request):
    context = await run_in_thread(_guides_payload)
    return templates.TemplateResponse(request, "guides.html", context)


def _build_guide_context(guide_id: str) -> dict:
    """Load + shape the guide context shared by the view and edit routes.

    Failure modes: HTTPException(404) when the guide id is unknown.
    """
    guide = _find_guide(guide_id)
    if not guide:
        raise HTTPException(status_code=404, detail="Guide not found")

    _normalize_tags(guide)
    if not isinstance(guide.get("steps"), list):
        guide["steps"] = []

    return {
        "active_page": "guides",
        "guide": guide,
        "steps": guide.get("steps", []),
        **action_menu_context(),
    }


@router.get("/guides/{guide_id}", response_class=HTMLResponse)
async def guide_detail(request: Request, guide_id: str):
    context = await run_in_thread(_build_guide_context, guide_id)
    context["initial_editing"] = False
    return templates.TemplateResponse(request, "guide_view.html", context)


@router.get("/guides/{guide_id}/edit", response_class=HTMLResponse)
async def guide_edit(request: Request, guide_id: str):
    context = await run_in_thread(_build_guide_context, guide_id)
    context["initial_editing"] = True
    return templates.TemplateResponse(request, "guide_edit.html", context)
