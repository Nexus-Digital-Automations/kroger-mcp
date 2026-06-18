"""API routes for guide write operations (technique how-tos)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger("kroger_mcp.web.guides")


def _check_if_match(guide: dict, if_match: str | None) -> JSONResponse | None:
    """409 when the client's view of updated_at is stale.

    Two tabs editing the same guide would otherwise last-write-wins on disk;
    the optimistic-locking header lets the UI detect and recover. Header is
    optional — calls without it keep last-write-wins.
    """
    if not if_match:
        return None
    current = (guide.get("updated_at") or "") or ""
    if if_match.strip('"') != current:
        return JSONResponse(
            status_code=409,
            content={
                "error": "Guide was edited elsewhere — refresh to see latest.",
                "current_updated_at": current,
            },
        )
    return None


def _clean_steps(steps: list[str]) -> list[str]:
    return [s.strip() for s in steps if s and s.strip()]


class CreateGuideBody(BaseModel):
    name: str = "Untitled guide"
    description: str | None = None
    steps: list[str] = []
    tags: list[str] = []
    time: str | None = None
    difficulty: str | None = None


@router.post("/api/guides")
async def create_guide(body: CreateGuideBody):
    """Create a new guide and return its id.

    Browser "New Guide" flow: POST with no steps to get an empty draft, then
    edit inline.
    """
    try:
        from kroger_mcp.tools.guide_tools import _load_guides, _save_guides

        guide_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        guide = {
            "id": guide_id,
            "name": body.name.strip() or "Untitled guide",
            "description": body.description,
            "steps": _clean_steps(body.steps),
            "tags": body.tags,
            "time": body.time,
            "difficulty": body.difficulty,
            "created_at": now,
            "updated_at": now,
        }
        store = _load_guides()
        store.setdefault("guides", []).append(guide)
        _save_guides(store)
        return {"success": True, "guide_id": guide_id, "updated_at": now}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


class UpdateGuideBody(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    time: str | None = None
    difficulty: str | None = None


@router.patch("/api/guides/{guide_id}")
async def update_guide(
    guide_id: str,
    body: UpdateGuideBody,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Update guide metadata (name, description, tags, time, difficulty)."""
    try:
        from kroger_mcp.tools.guide_tools import _load_guides, _save_guides

        data = _load_guides()
        guide = next(
            (g for g in data.get("guides", []) if g.get("id") == guide_id),
            None,
        )
        if not guide:
            return JSONResponse(
                status_code=404,
                content={"error": f"Guide '{guide_id}' not found"},
            )
        conflict = _check_if_match(guide, if_match)
        if conflict is not None:
            return conflict
        if body.name is not None:
            guide["name"] = body.name
        if body.description is not None:
            guide["description"] = body.description
        if body.tags is not None:
            guide["tags"] = body.tags
        if body.time is not None:
            guide["time"] = body.time
        if body.difficulty is not None:
            guide["difficulty"] = body.difficulty
        guide["updated_at"] = datetime.now().isoformat()
        _save_guides(data)
        return {"success": True, "guide_id": guide_id, "updated_at": guide["updated_at"]}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


class ReplaceStepsBody(BaseModel):
    steps: list[str]


@router.put("/api/guides/{guide_id}/steps")
async def replace_guide_steps(
    guide_id: str,
    body: ReplaceStepsBody,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Replace the ordered step list for a guide (add/edit/remove/reorder)."""
    try:
        from kroger_mcp.tools.guide_tools import _load_guides, _save_guides

        data = _load_guides()
        guide = next(
            (g for g in data.get("guides", []) if g.get("id") == guide_id),
            None,
        )
        if not guide:
            return JSONResponse(
                status_code=404,
                content={"error": f"Guide '{guide_id}' not found"},
            )
        conflict = _check_if_match(guide, if_match)
        if conflict is not None:
            return conflict
        guide["steps"] = _clean_steps(body.steps)
        guide["updated_at"] = datetime.now().isoformat()
        _save_guides(data)
        return {
            "success": True,
            "guide_id": guide_id,
            "step_count": len(guide["steps"]),
            "updated_at": guide["updated_at"],
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/guides/{guide_id}")
async def delete_guide(guide_id: str):
    """Delete a guide by ID."""
    try:
        from kroger_mcp.tools.guide_tools import _load_guides, _save_guides

        data = _load_guides()
        original_count = len(data.get("guides", []))
        data["guides"] = [g for g in data.get("guides", []) if g.get("id") != guide_id]
        if len(data["guides"]) == original_count:
            return JSONResponse(
                status_code=404,
                content={"error": f"Guide '{guide_id}' not found"},
            )
        _save_guides(data)
        return {"success": True, "guide_id": guide_id}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
