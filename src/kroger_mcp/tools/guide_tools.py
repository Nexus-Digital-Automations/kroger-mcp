"""
Cooking-guide management tools.

Guides are technique / how-to references (soaking beans, knife skills, making
bread or noodles) — distinct from recipes: no ingredients to buy, no servings,
cost, cart, or meal-plan. A guide is name + description + ordered steps + tags +
an optional time/difficulty.

Storage mirrors recipe_tools: a JSON file (kroger_guides.json) behind a JsonStore
with fingerprint memoization. The web layer imports _load_guides/_save_guides/
_find_guide directly, the same way it consumes recipes.
"""

import asyncio
import copy
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from ._storage import JsonStore

logger = logging.getLogger(__name__)

# Guide storage file (sibling of kroger_recipes.json at the repo root)
_BASE_DIR = Path(__file__).parent.parent.parent.parent  # → kroger-mcp/
GUIDES_FILE = str(_BASE_DIR / "kroger_guides.json")

_guides_store = JsonStore(GUIDES_FILE, default=lambda: {"guides": [], "last_updated": None})

# Memoize by file fingerprint (mtime_ns + size) — same hot-path optimization and
# deep-copy contract as recipe_tools._load_recipes: callers mutate the returned
# dict, so the cached master must stay pristine.
_guides_cache: tuple[tuple[int, int], dict[str, Any]] | None = None


def _load_guides() -> dict[str, Any]:
    """Load guides, skipping the disk re-read when the file is unchanged."""
    global _guides_cache
    try:
        st = _guides_store.path.stat()
    except OSError:
        return _guides_store.load()

    fingerprint = (st.st_mtime_ns, st.st_size)
    if _guides_cache is not None and _guides_cache[0] == fingerprint:
        return copy.deepcopy(_guides_cache[1])

    data = _guides_store.load()
    _guides_cache = (fingerprint, data)
    return copy.deepcopy(data)


def _save_guides(data: dict[str, Any]) -> None:
    data["last_updated"] = datetime.now().isoformat()
    try:
        _guides_store.save(data)
    except OSError as exc:
        logger.warning("Could not save guides: %s", exc)


def _find_guide(guide_id: str) -> dict[str, Any] | None:
    """Find a guide by ID."""
    data = _load_guides()
    for guide in data.get("guides", []):
        if guide.get("id") == guide_id:
            return guide
    return None


def _normalize_steps(steps: Any) -> list[str]:
    """Coerce steps to a clean list of non-empty strings."""
    if steps is None:
        return []
    if isinstance(steps, str):
        # Allow a newline-delimited block as a convenience.
        steps = steps.replace("\\n", "\n").split("\n")
    return [str(s).strip() for s in steps if str(s).strip()]


def register_tools(mcp):
    """Register guide-related tools with the FastMCP server."""

    @mcp.tool()
    async def guides(
        action: Literal[
            "list",
            "get",
            "save",
            "update",
            "delete",
            "search",
        ] = Field(
            description=(
                "save — create a technique guide (name + steps required). "
                "Other: list|get|update|delete|search"
            )
        ),
        guide_id: str | None = Field(default=None, description="Guide ID"),
        name: str | None = Field(default=None, description="Guide name"),
        description: str | None = Field(default=None, description="Brief description"),
        steps: list[str] | None = Field(
            default=None,
            description="Ordered list of step strings (one instruction each)",
        ),
        tags: list[str] | None = Field(default=None, description="Tags for categorization"),
        time: str | None = Field(
            default=None,
            description="Human-readable time, e.g. '8–12 h' or '15 min'",
        ),
        difficulty: str | None = Field(
            default=None,
            description="easy | medium | hard",
        ),
        limit: int | None = Field(default=20, description="Max guides to return"),
        tag_filter: str | None = Field(default=None, description="Filter by tag"),
        query: str | None = Field(default=None, description="Search term"),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Cooking-guide management (techniques / how-tos).

        A guide holds name, description, ordered steps, tags, and an optional
        time/difficulty. No ingredients, cost, cart, or meal-plan — guides are
        reference material, not meals.
        """
        return await asyncio.to_thread(
            _guides_impl,
            action,
            guide_id,
            name,
            description,
            steps,
            tags,
            time,
            difficulty,
            limit,
            tag_filter,
            query,
            ctx,
        )

    def _guides_impl(
        action,
        guide_id,
        name,
        description,
        steps,
        tags,
        time,
        difficulty,
        limit,
        tag_filter,
        query,
        ctx,
    ):
        match action:
            case "save":
                if not name:
                    return {"success": False, "error": "name is required"}
                clean_steps = _normalize_steps(steps)
                if not clean_steps:
                    return {"success": False, "error": "At least one step is required"}

                guide_id_new = str(uuid.uuid4())[:8]
                now = datetime.now().isoformat()
                guide = {
                    "id": guide_id_new,
                    "name": name,
                    "description": description,
                    "steps": clean_steps,
                    "tags": tags or [],
                    "time": time,
                    "difficulty": difficulty,
                    "created_at": now,
                    "updated_at": now,
                }

                data = _load_guides()
                data["guides"].append(guide)
                _save_guides(data)

                if ctx:
                    ctx.info(f"Saved guide '{name}' with {len(clean_steps)} steps")

                return {
                    "success": True,
                    "guide_id": guide_id_new,
                    "message": f"Guide '{name}' saved successfully",
                    "step_count": len(clean_steps),
                }

            case "list":
                try:
                    data = _load_guides()
                    guide_list = data.get("guides", [])

                    if tag_filter:
                        tag_lower = tag_filter.lower()
                        guide_list = [
                            g
                            for g in guide_list
                            if any(tag_lower in t.lower() for t in g.get("tags", []))
                        ]

                    guide_list = sorted(
                        guide_list,
                        key=lambda g: g.get("created_at", ""),
                        reverse=True,
                    )[: (limit or 20)]

                    summaries = [
                        {
                            "id": g["id"],
                            "name": g["name"],
                            "description": g.get("description"),
                            "step_count": len(g.get("steps", [])),
                            "tags": g.get("tags", []),
                            "time": g.get("time"),
                            "difficulty": g.get("difficulty"),
                            "created_at": g.get("created_at"),
                        }
                        for g in guide_list
                    ]

                    return {
                        "success": True,
                        "guides": summaries,
                        "count": len(summaries),
                        "total_saved": len(data.get("guides", [])),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get guides: {str(e)}"}

            case "get":
                if not guide_id:
                    return {"success": False, "error": "guide_id is required"}
                try:
                    guide = _find_guide(guide_id)
                    if not guide:
                        return {"success": False, "error": f"Guide '{guide_id}' not found"}
                    return {"success": True, "guide": guide}
                except Exception as e:
                    return {"success": False, "error": f"Failed to get guide: {str(e)}"}

            case "update":
                if not guide_id:
                    return {"success": False, "error": "guide_id is required"}
                try:
                    data = _load_guides()
                    found = False
                    for guide in data.get("guides", []):
                        if guide.get("id") == guide_id:
                            found = True
                            if name is not None:
                                guide["name"] = name
                            if description is not None:
                                guide["description"] = description
                            if steps is not None:
                                guide["steps"] = _normalize_steps(steps)
                            if tags is not None:
                                guide["tags"] = tags
                            if time is not None:
                                guide["time"] = time
                            if difficulty is not None:
                                guide["difficulty"] = difficulty
                            guide["updated_at"] = datetime.now().isoformat()
                            break

                    if not found:
                        return {"success": False, "error": f"Guide '{guide_id}' not found"}

                    _save_guides(data)
                    return {
                        "success": True,
                        "message": f"Guide '{guide_id}' updated",
                        "guide_id": guide_id,
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to update guide: {str(e)}"}

            case "delete":
                if not guide_id:
                    return {"success": False, "error": "guide_id is required"}
                try:
                    data = _load_guides()
                    original_count = len(data.get("guides", []))
                    data["guides"] = [
                        g for g in data.get("guides", []) if g.get("id") != guide_id
                    ]
                    if len(data["guides"]) == original_count:
                        return {"success": False, "error": f"Guide '{guide_id}' not found"}
                    _save_guides(data)
                    return {
                        "success": True,
                        "message": f"Guide '{guide_id}' deleted",
                        "remaining_guides": len(data["guides"]),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to delete guide: {str(e)}"}

            case "search":
                if not query:
                    return {"success": False, "error": "query is required"}
                try:
                    data = _load_guides()
                    query_lower = query.lower()
                    matches = []
                    for guide in data.get("guides", []):
                        if query_lower in guide.get("name", "").lower():
                            matches.append(guide)
                            continue
                        if any(query_lower in tag.lower() for tag in guide.get("tags", [])):
                            matches.append(guide)
                            continue
                        if query_lower in (guide.get("description") or "").lower():
                            matches.append(guide)

                    summaries = [
                        {
                            "id": g["id"],
                            "name": g["name"],
                            "description": g.get("description"),
                            "tags": g.get("tags", []),
                            "step_count": len(g.get("steps", [])),
                        }
                        for g in matches
                    ]

                    return {
                        "success": True,
                        "query": query,
                        "matches": summaries,
                        "count": len(summaries),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to search: {str(e)}"}

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
