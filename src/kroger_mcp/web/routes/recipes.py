"""Recipe routes — list and detail views."""

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from kroger_mcp.analytics.database import run_in_thread
from kroger_mcp.analytics.recipe_cost import estimate_recipe_cost
from kroger_mcp.analytics.recipe_scoring import calculate_health_score
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.tools.recipe_tools import _find_recipe, _load_recipes
from kroger_mcp.tools.step_times import annotate_steps, recipe_time_summary
from kroger_mcp.web.context import action_menu_context
from kroger_mcp.web.templating import templates

router = APIRouter()


def _parse_instructions(text: str) -> list[dict]:
    """Parse raw instruction string into grouped sections + steps."""
    if not text:
        return []
    # Handle JSON-array-encoded instructions: ["step1", "step2", ...]
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            steps = json.loads(stripped)
            if isinstance(steps, list) and all(isinstance(s, str) for s in steps):
                cleaned = [_clean_instruction_step(s) or s.strip() for s in steps if s.strip()]
                return [{"header": None, "steps": cleaned}] if cleaned else []
        except (json.JSONDecodeError, ValueError):
            pass
    # Normalize literal \n escape sequences to real newlines
    text = text.replace("\\n", "\n")
    groups: list[dict[str, Any]] = []
    current_header: str | None = None
    current_steps: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if _is_instruction_header(line):
            if current_steps:
                groups.append({"header": current_header, "steps": current_steps})
                current_steps = []
            current_header = _clean_instruction_header(line)
        else:
            step = _clean_instruction_step(line)
            if step:
                current_steps.append(step)
    if current_steps:
        groups.append({"header": current_header, "steps": current_steps})
    return groups


def _is_instruction_header(line: str) -> bool:
    if re.match(r"^Step\s+\d+\s*[\u2013\u2014:\-]", line):
        return True
    if re.match(r"^\*\*[^*]+\*\*:?\s*$", line):
        return True
    if re.match(r"^[A-Z][A-Z0-9\s\(\)\/]+:\s*$", line):
        return True
    return False


def _clean_instruction_header(line: str) -> str:
    # Remove Step X: prefix
    line = re.sub(r"^Step\s+\d+\s*[\u2013\u2014:\-]\s*", "", line)
    # Remove bold markers
    line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
    # Remove trailing colon
    line = re.sub(r":\s*$", "", line)
    return line.strip()


def _clean_instruction_step(line: str) -> str:
    # Remove bullet/asterisk
    line = re.sub(r"^[\*\-\u2022]\s*", "", line)
    # Remove numbered prefix (1., 2., etc.)
    line = re.sub(r"^\d+[\.\)]\s*", "", line)
    return line.strip()


def _collect_all_tags(recipes: list[dict]) -> list[str]:
    """Return sorted unique tags from all recipes."""
    tags = set()
    for r in recipes:
        for t in r.get("tags", []):
            if isinstance(t, str) and t.strip():
                tags.add(t.strip())
    return sorted(tags)


def _recipes_payload(user_id: str) -> dict:
    """All blocking work for the recipes list (JSON load + per-recipe scoring),
    run off the event loop via run_in_thread."""
    data = _load_recipes()
    recipes = data.get("recipes", [])

    # Normalize tags to always be a list
    for r in recipes:
        if isinstance(r.get("tags"), str):
            r["tags"] = [t.strip() for t in r["tags"].split(",") if t.strip()]
        elif not r.get("tags"):
            r["tags"] = []

    all_tags = _collect_all_tags(recipes)

    # Effective time summary for cards + the Quickest sort (cheap regex parse;
    # explicit total_time_minutes wins over the derived step sum).
    for r in recipes:
        try:
            r["_time"] = recipe_time_summary(r)
        except Exception:
            r["_time"] = {"total": 0, "active": 0, "passive": 0, "label": ""}

    # Compute cost per serving and health score for each recipe
    for r in recipes:
        try:
            cost_data = estimate_recipe_cost(r)
            r["cost_per_serving"] = cost_data.get("cost_per_serving")
        except Exception:
            r["cost_per_serving"] = None
        try:
            health = calculate_health_score(r, names_only=True, user_id=user_id)
            r["health_score"] = health["score"]
            r["health_grade"] = health["grade"]
            r["health_flags"] = health.get("flags", [])
            r["health_categories"] = health.get("categories_detected", [])
            r["health_bonus"] = health.get("bonus_applied", 0)
        except Exception:
            r["health_score"] = None
            r["health_grade"] = None
            r["health_flags"] = []
            r["health_categories"] = []
            r["health_bonus"] = 0

    # Build array for Alpine x-for rendering (serialized via Jinja's |tojson
    # in the template, not here — a pre-dumped string piped through |safe
    # was a stored-XSS vector for recipe names containing HTML/script markup)
    recipes_json = [
        {
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "servings": r.get("servings"),
            "ing_count": len(r.get("ingredients", [])),
            "tags": r.get("tags", []),
            "times_ordered": r.get("times_ordered") or 0,
            "cost": r.get("cost_per_serving"),
            "health_score": r.get("health_score"),
            "health_grade": r.get("health_grade"),
            "health_flags": r.get("health_flags", []),
            "health_categories": r.get("health_categories", []),
            "health_bonus": r.get("health_bonus", 0),
            "time_total": r["_time"]["total"],
            "time_label": r["_time"]["label"],
            "time_passive": r["_time"]["passive"],
        }
        for r in recipes
    ]

    return {
        "active_page": "recipes",
        "recipes": recipes,
        "all_tags": all_tags,
        "recipe_count": len(recipes),
        "recipes_json": recipes_json,
        **action_menu_context(user_id),
    }


@router.get("/recipes", response_class=HTMLResponse)
async def recipes_list(request: Request):
    context = await run_in_thread(_recipes_payload, current_user_id(request))
    return templates.TemplateResponse(request, "recipes.html", context)


_ATTR_TO_CATEGORY = {
    "Fresh Produce": "produce",
    "Fresh Fruit": "produce",
    "Lean Protein": "meat",
    "Healthy Fat": "pantry",
    "Whole Grain": "pantry",
    "Herb or Spice": "produce",
    "Natural Dairy": "dairy",
    "Pantry Staple": "pantry",
}


def enrich_ingredients_for_view(request: Request, ingredients: list[dict]) -> list[dict]:
    """Decorate ingredient dicts with safety, inferred category, and pantry status.

    Single source of truth for both the page-render route and the JSON
    refresh endpoint. Mutates in place AND returns the list — callers
    that already hold the same list reference get both behaviors.
    Failures are non-fatal; missing chips simply don't render.
    """
    _annotate_safety(ingredients, user_id=current_user_id(request))
    _infer_categories_from_safety(ingredients)
    _annotate_pantry(request, ingredients)
    return ingredients


def _annotate_safety(ingredients: list[dict], *, user_id: str) -> None:
    try:
        from kroger_mcp.analytics.database import get_db_connection
        from kroger_mcp.analytics.ingredients import check_product_safety

        product_descs: dict[str, str] = {}
        linked_ids = [ing["product_id"] for ing in ingredients if ing.get("product_id")]
        if linked_ids:
            try:
                conn = get_db_connection()
                try:
                    placeholders = ",".join("?" * len(linked_ids))
                    rows = conn.execute(
                        f"SELECT product_id, description, brand FROM products WHERE product_id IN ({placeholders})",
                        linked_ids,
                    ).fetchall()
                    for row in rows:
                        desc = row["description"] or ""
                        if row["brand"]:
                            desc = row["brand"] + " " + desc
                        product_descs[row["product_id"]] = desc
                finally:
                    conn.close()
            except Exception:
                pass

        for ing in ingredients:
            pid = ing.get("product_id")
            scan_text = product_descs.get(pid, "") if pid else ""
            if not scan_text:
                scan_text = ing.get("name", "")
            result = check_product_safety(scan_text, user_id=user_id)
            ing["safety_score"] = result.score
            ing["safety_grade"] = result.grade
            ing["safety_flags"] = [
                {
                    "ingredient": m.ingredient_name,
                    "severity": m.severity.value,
                    "reason": m.reason,
                    "category": m.category,
                }
                for m in result.matches
            ]
            ing["safety_positives"] = [
                {"attribute": a.attribute_name, "bonus": a.bonus, "benefit": a.benefit}
                for a in result.positive_attributes
            ]
    except Exception:
        pass


def _infer_categories_from_safety(ingredients: list[dict]) -> None:
    for ing in ingredients:
        if ing.get("category"):
            continue
        for pos in ing.get("safety_positives", []) or []:
            cat = _ATTR_TO_CATEGORY.get(pos.get("attribute"))
            if cat:
                ing["category"] = cat
                break


def _annotate_pantry(request: Request, ingredients: list[dict]) -> None:
    try:
        from kroger_mcp.analytics.pantry import get_pantry_status

        pantry_lookup = {
            item["product_id"]: item
            for item in get_pantry_status(apply_depletion=True, user_id=current_user_id(request))
        }
        for ing in ingredients:
            pid = ing.get("product_id")
            if not pid:
                ing["pantry"] = None
                continue
            stocked = pantry_lookup.get(pid)
            if not stocked:
                ing["pantry"] = None
                continue
            ing["pantry"] = {
                "level_percent": stocked.get("level_percent"),
                "status": stocked.get("status"),
                "quantity_on_hand": stocked.get("quantity_on_hand"),
                "unit": stocked.get("unit"),
            }
    except Exception:
        for ing in ingredients:
            ing.setdefault("pantry", None)


def _build_recipe_context(request: Request, recipe_id: str, include_spices: bool = False) -> dict:
    """Load + shape the recipe context shared by the view and edit routes.

    Failure modes: HTTPException(404) when the recipe id is unknown; health
    score, cost estimate, and ingredient enrichment are best-effort and never
    raise. ``include_spices`` folds spices into the cost total when truthy.
    """
    recipe = _find_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    if isinstance(recipe.get("tags"), str):
        recipe["tags"] = [t.strip() for t in recipe["tags"].split(",") if t.strip()]
    elif not recipe.get("tags"):
        recipe["tags"] = []

    ingredients = recipe.get("ingredients", [])
    for ing in ingredients:
        ing.setdefault("product_id", None)
        ing.setdefault("override", False)

    instruction_groups = _parse_instructions(recipe.get("instructions") or "")

    # Per-step time annotations, aligned to the same flattening the template
    # (and instructionsEditor) uses: headers interleaved with steps.
    flat_steps: list[str] = []
    for grp in instruction_groups:
        if grp.get("header"):
            flat_steps.append(grp["header"])
        flat_steps.extend(grp.get("steps") or [])
    step_time_data = annotate_steps(flat_steps, recipe.get("step_times"))

    health_data = None
    try:
        health_data = calculate_health_score(recipe, user_id=current_user_id(request))
    except Exception:
        pass

    cost_data = None
    try:
        cost_data = estimate_recipe_cost(recipe, include_spices=include_spices)
    except Exception:
        pass

    enrich_ingredients_for_view(request, ingredients)

    return {
        "active_page": "recipes",
        "recipe": recipe,
        "ingredients": ingredients,
        "instruction_groups": instruction_groups,
        "step_time_data": step_time_data,
        "health_data": health_data,
        "cost_data": cost_data,
        "include_spices": include_spices,
        **action_menu_context(current_user_id(request)),
    }


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
async def recipe_detail(request: Request, recipe_id: str, include_spices: bool = False):
    context = await run_in_thread(_build_recipe_context, request, recipe_id, include_spices)
    context["initial_editing"] = False
    return templates.TemplateResponse(request, "recipe_view.html", context)


@router.get("/recipes/{recipe_id}/edit", response_class=HTMLResponse)
async def recipe_edit(request: Request, recipe_id: str):
    context = await run_in_thread(_build_recipe_context, request, recipe_id)
    context["initial_editing"] = True
    return templates.TemplateResponse(request, "recipe_edit.html", context)
