"""Recipe routes — list and detail views."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.tools.recipe_tools import _find_recipe, _load_recipes

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


def _parse_instructions(text: str) -> list[dict]:
    """Parse raw instruction string into grouped sections + steps."""
    if not text:
        return []
    groups = []
    current_header = None
    current_steps = []
    for line in text.split('\n'):
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
    if re.match(r'^Step\s+\d+\s*[–—:\-]', line):
        return True
    if re.match(r'^\*\*[^*]+\*\*:?\s*$', line):
        return True
    if re.match(r'^[A-Z][A-Z0-9\s\(\)\/]+:\s*$', line):
        return True
    return False


def _clean_instruction_header(line: str) -> str:
    line = re.sub(r'\*\*', '', line)
    return line.rstrip(':').strip()


def _clean_instruction_step(line: str) -> str:
    line = re.sub(r'^\d+\.\s+', '', line)
    line = re.sub(r'\*\*', '', line)
    return line.strip()


def _collect_all_tags(recipes):
    tags = set()
    for r in recipes:
        for tag in r.get("tags", []):
            if tag:
                tags.add(tag)
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

    return templates.TemplateResponse("recipes.html", {
        "request": request,
        "active_page": "recipes",
        "recipes": recipes,
        "all_tags": all_tags,
        "recipe_count": len(recipes),
    })


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

    return templates.TemplateResponse("recipe_detail.html", {
        "request": request,
        "active_page": "recipes",
        "recipe": recipe,
        "ingredients": ingredients,
        "instruction_groups": instruction_groups,
    })
