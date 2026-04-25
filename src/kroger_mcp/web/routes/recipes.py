"""Recipe routes — list and detail views."""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.recipe_scoring import calculate_health_score, estimate_recipe_cost
from kroger_mcp.tools.recipe_tools import _find_recipe, _load_recipes
from kroger_mcp.web.context import action_menu_context

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
    groups = []
    current_header = None
    current_steps = []
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


@router.get("/recipes", response_class=HTMLResponse)
async def recipes_list(request: Request):
    data = _load_recipes()
    recipes = data.get("recipes", [])

    # Normalize tags to always be a list
    for r in recipes:
        if isinstance(r.get("tags"), str):
            r["tags"] = [t.strip() for t in r["tags"].split(",") if t.strip()]
        elif not r.get("tags"):
            r["tags"] = []

    all_tags = _collect_all_tags(recipes)

    # Compute cost per serving and health score for each recipe
    for r in recipes:
        try:
            cost_data = estimate_recipe_cost(r)
            r["cost_per_serving"] = cost_data.get("cost_per_serving")
        except Exception:
            r["cost_per_serving"] = None
        try:
            health = calculate_health_score(r, names_only=True)
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

    # Build JSON array for Alpine x-for rendering
    recipes_json = json.dumps(
        [
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
            }
            for r in recipes
        ]
    )

    return templates.TemplateResponse(
        "recipes.html",
        {
            "request": request,
            "active_page": "recipes",
            "recipes": recipes,
            "all_tags": all_tags,
            "recipe_count": len(recipes),
            "recipes_json": recipes_json,
            **action_menu_context(),
        },
    )


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
async def recipe_detail(request: Request, recipe_id: str):
    recipe = _find_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    # Normalize tags
    if isinstance(recipe.get("tags"), str):
        recipe["tags"] = [t.strip() for t in recipe["tags"].split(",") if t.strip()]
    elif not recipe.get("tags"):
        recipe["tags"] = []

    # Normalize ingredients
    ingredients = recipe.get("ingredients", [])
    for ing in ingredients:
        ing.setdefault("product_id", None)
        ing.setdefault("override", False)

    instruction_groups = _parse_instructions(recipe.get("instructions") or "")

    # Overall recipe health score
    health_data = None
    try:
        health_data = calculate_health_score(recipe)
    except Exception:
        pass

    # Per-ingredient safety — use product descriptions from DB when available
    try:
        from kroger_mcp.analytics.database import get_db_connection
        from kroger_mcp.analytics.ingredients import check_product_safety

        # Batch-load product descriptions for linked ingredients
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
            result = check_product_safety(scan_text)
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

    # Auto-infer category from safety positive attributes when missing
    _attr_to_cat = {
        "Fresh Produce": "produce",
        "Fresh Fruit": "produce",
        "Lean Protein": "meat",
        "Healthy Fat": "pantry",
        "Whole Grain": "pantry",
        "Herb or Spice": "produce",
        "Natural Dairy": "dairy",
        "Pantry Staple": "pantry",
    }
    for ing in ingredients:
        if not ing.get("category"):
            for pos in ing.get("safety_positives", []):
                cat = _attr_to_cat.get(pos.get("attribute"))
                if cat:
                    ing["category"] = cat
                    break

    return templates.TemplateResponse(
        "recipe_detail.html",
        {
            "request": request,
            "active_page": "recipes",
            "recipe": recipe,
            "ingredients": ingredients,
            "instruction_groups": instruction_groups,
            "health_data": health_data,
        },
    )
