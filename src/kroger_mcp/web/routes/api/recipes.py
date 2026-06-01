"""API routes for recipe write operations."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics.ingredient_links import (
    normalize_ingredient_name,
    record_link,
)
from kroger_mcp.auth.dependencies import current_user_id

router = APIRouter()
logger = logging.getLogger("kroger_mcp.web.recipes")


def _record_ingredient_links(
    user_id: str, before: list[dict], after: list[IngredientIn]
) -> None:
    """Teach the per-account link memory from a recipe's ingredient save.

    Records only links that are new or changed since `before` — an ingredient
    whose (normalized name, product_id) pair wasn't already on the recipe — so
    ordinary re-saves don't inflate link counts. Best-effort; record_link
    itself never raises.
    """
    prior = {
        (normalize_ingredient_name(ing.get("name", "")), ing.get("product_id"))
        for ing in before
        if ing.get("product_id")
    }
    for ing in after:
        if not ing.product_id:
            continue
        if (normalize_ingredient_name(ing.name), ing.product_id) in prior:
            continue
        # ing.name is the best human-readable label we have here: after a popover
        # link it holds the Kroger product description; the payload carries no
        # separate description field. (Category is NOT a description.)
        record_link(user_id, ing.name, ing.product_id, ing.name)


def _teach_link_memory(
    request: Request, before: list[dict], after: list[IngredientIn]
) -> None:
    """Resolve the account and record new links — fully best-effort.

    Runs after the save has already succeeded, so neither an unauthenticated
    request (401 from current_user_id) nor any memory failure may surface as an
    error to the caller. The recipe is saved regardless.
    """
    try:
        _record_ingredient_links(current_user_id(request), before, after)
    except Exception:
        logger.warning("ingredient_link.teach_skipped", exc_info=True)


class IngredientIn(BaseModel):
    name: str
    quantity: float | None = None
    unit: str | None = None
    category: str | None = None
    product_id: str | None = None
    override: bool = False
    override_reason: str | None = None


class ReplaceIngredientsBody(BaseModel):
    ingredients: list[IngredientIn]


class ReplaceInstructionsBody(BaseModel):
    instructions: list[str]


class AddToCartBody(BaseModel):
    confirm: bool = False
    modality: str = "PICKUP"


def _check_if_match(recipe: dict, if_match: str | None) -> JSONResponse | None:
    """409 when the client's view of updated_at is stale.

    Why: two tabs editing the same recipe last-write-wins on disk; the
    optimistic-locking header lets the UI detect and recover instead of
    silently clobbering a peer's save. Header is optional — calls without
    it keep the legacy behavior.
    """
    if not if_match:
        return None
    current = (recipe.get("updated_at") or "") or ""
    if if_match.strip('"') != current:
        return JSONResponse(
            status_code=409,
            content={
                "error": "Recipe was edited elsewhere — refresh to see latest.",
                "current_updated_at": current,
            },
        )
    return None


@router.put("/api/recipes/{recipe_id}/ingredients")
async def replace_recipe_ingredients(
    recipe_id: str,
    body: ReplaceIngredientsBody,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Replace the entire ingredient list for a recipe.

    Atomic alternative to the append-one POST endpoint. The recipe-detail
    inline editor uses this so add/edit/remove/reorder flow through a
    single round-trip. Safety scoring is re-derived at render time, so we
    do not persist it here.

    The recipe itself stays global, but any newly-linked ingredient teaches
    the calling account's private link memory (smart auto-link / "your usuals").
    """
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes, _save_recipes

        store = _load_recipes()
        recipe = next(
            (r for r in store.get("recipes", []) if r.get("id") == recipe_id),
            None,
        )
        if not recipe:
            return JSONResponse(
                status_code=404,
                content={"error": f"Recipe '{recipe_id}' not found"},
            )
        conflict = _check_if_match(recipe, if_match)
        if conflict is not None:
            return conflict
        before = list(recipe.get("ingredients") or [])
        recipe["ingredients"] = [
            {
                "name": ing.name,
                "quantity": ing.quantity,
                "unit": ing.unit,
                "category": ing.category,
                "product_id": ing.product_id,
                "override": ing.override,
                "override_reason": ing.override_reason,
            }
            for ing in body.ingredients
        ]
        recipe["updated_at"] = datetime.now().isoformat()
        _save_recipes(store)
        _teach_link_memory(request, before, body.ingredients)
        return {
            "success": True,
            "recipe_id": recipe_id,
            "ingredient_count": len(recipe["ingredients"]),
            "updated_at": recipe["updated_at"],
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.put("/api/recipes/{recipe_id}/instructions")
async def replace_recipe_instructions(
    recipe_id: str,
    body: ReplaceInstructionsBody,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Replace the recipe's instruction steps.

    Stored as a JSON-encoded list so the existing `_parse_instructions`
    reader (which already handles the JSON-array form) round-trips
    cleanly without a schema migration.
    """
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes, _save_recipes

        store = _load_recipes()
        recipe = next(
            (r for r in store.get("recipes", []) if r.get("id") == recipe_id),
            None,
        )
        if not recipe:
            return JSONResponse(
                status_code=404,
                content={"error": f"Recipe '{recipe_id}' not found"},
            )
        conflict = _check_if_match(recipe, if_match)
        if conflict is not None:
            return conflict
        steps = [s.strip() for s in body.instructions if s and s.strip()]
        recipe["instructions"] = json.dumps(steps)
        recipe["updated_at"] = datetime.now().isoformat()
        _save_recipes(store)
        return {
            "success": True,
            "recipe_id": recipe_id,
            "step_count": len(steps),
            "updated_at": recipe["updated_at"],
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/recipes/{recipe_id}")
async def delete_recipe(recipe_id: str):
    """Delete a recipe by ID."""
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes, _save_recipes

        data = _load_recipes()
        original_count = len(data.get("recipes", []))
        data["recipes"] = [r for r in data.get("recipes", []) if r.get("id") != recipe_id]
        if len(data["recipes"]) == original_count:
            return JSONResponse(
                status_code=404,
                content={"error": f"Recipe '{recipe_id}' not found"},
            )
        _save_recipes(data)
        return {"success": True, "recipe_id": recipe_id}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


class UpdateRecipeBody(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    servings: int | None = None


class AddIngredientBody(BaseModel):
    # Kroger product id (aka UPC). Required — callers pair a Kroger product
    # with an existing recipe so that later recipe-to-cart flows can locate
    # the SKU. Manual (non-Kroger) ingredients are authored elsewhere.
    product_id: str
    description: str
    quantity: float | None = None
    unit: str | None = None
    brand: str | None = None
    category: str | None = None


@router.post("/api/recipes/{recipe_id}/ingredients")
async def add_ingredient_to_recipe(recipe_id: str, body: AddIngredientBody):
    """
    Append a Kroger product as an ingredient on an existing recipe.

    Counterpart: templates/_macros/action_menu.html — the "Add to Recipe"
    submenu on product cards dispatches 'action-menu:recipe-add' and the
    host page POSTs here.

    Returns 404 when the recipe id is not found — the caller's recipe list
    is stale (e.g., recipe was deleted in another tab).
    """
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes, _save_recipes

        data = _load_recipes()
        recipe = next(
            (r for r in data.get("recipes", []) if r.get("id") == recipe_id),
            None,
        )
        if not recipe:
            return JSONResponse(
                status_code=404,
                content={"error": f"Recipe '{recipe_id}' not found"},
            )
        ingredients = recipe.setdefault("ingredients", [])
        ingredients.append(
            {
                "name": body.description,
                "quantity": body.quantity,
                "unit": body.unit,
                "category": body.category,
                "product_id": body.product_id,
                "override": False,
                "override_reason": None,
            }
        )
        recipe["updated_at"] = datetime.now().isoformat()
        _save_recipes(data)
        return {
            "success": True,
            "recipe_id": recipe_id,
            "recipe_name": recipe.get("name"),
            "ingredient_count": len(ingredients),
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.patch("/api/recipes/{recipe_id}")
async def update_recipe(
    recipe_id: str,
    body: UpdateRecipeBody,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Update recipe metadata (name, description, tags, servings)."""
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes, _save_recipes

        data = _load_recipes()
        recipe = next(
            (r for r in data.get("recipes", []) if r.get("id") == recipe_id),
            None,
        )
        if not recipe:
            return JSONResponse(
                status_code=404,
                content={"error": f"Recipe '{recipe_id}' not found"},
            )
        conflict = _check_if_match(recipe, if_match)
        if conflict is not None:
            return conflict
        if body.name is not None:
            recipe["name"] = body.name
        if body.description is not None:
            recipe["description"] = body.description
        if body.tags is not None:
            recipe["tags"] = body.tags
        if body.servings is not None:
            recipe["servings"] = body.servings
        recipe["updated_at"] = datetime.now().isoformat()
        _save_recipes(data)
        return {
            "success": True,
            "recipe_id": recipe_id,
            "updated_at": recipe["updated_at"],
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


class CreateRecipeBody(BaseModel):
    name: str = "Untitled recipe"
    description: str | None = None
    servings: int = 4
    tags: list[str] = []
    ingredients: list[IngredientIn] = []
    instructions: list[str] = []
    source: str | None = "user provided"


@router.post("/api/recipes")
async def create_recipe(request: Request, body: CreateRecipeBody):
    """Create a new recipe and return its id.

    Browser "New Recipe" flow: POST with no ingredients/instructions to
    get an empty draft, then edit inline. When ingredients are present,
    they go through the same product_id-or-override validation as the
    MCP `recipes(action='save')` tool — single source of truth.
    """
    try:
        from kroger_mcp.tools.recipe_tools import (
            _load_recipes,
            _normalize_ingredients,
            _save_recipes,
            _trigger_notion_sync,
            _validate_ingredients,
        )

        ingredient_dicts = [ing.model_dump() for ing in body.ingredients]
        if ingredient_dicts:
            errors = _validate_ingredients(ingredient_dicts)
            if errors:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "Recipe ingredients require Kroger product IDs",
                        "validation_errors": errors,
                    },
                )
        recipe_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        recipe = {
            "id": recipe_id,
            "name": body.name.strip() or "Untitled recipe",
            "description": body.description,
            "servings": body.servings,
            "ingredients": _normalize_ingredients(ingredient_dicts),
            "instructions": (
                json.dumps([s.strip() for s in body.instructions if s and s.strip()])
                if body.instructions
                else None
            ),
            "source": body.source or "user provided",
            "tags": body.tags,
            "created_at": now,
            "updated_at": now,
            "last_ordered_at": None,
            "times_ordered": 0,
        }
        store = _load_recipes()
        store.setdefault("recipes", []).append(recipe)
        _save_recipes(store)
        _trigger_notion_sync("push", recipe)
        return {"success": True, "recipe_id": recipe_id, "updated_at": now}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/recipes/{recipe_id}/ingredients")
async def get_recipe_ingredients(request: Request, recipe_id: str):
    """Return the recipe's ingredients with safety + pantry enrichment.

    Powers the post-save refresh used by the inline editor: cheaper than
    re-rendering the whole HTML page and avoids losing scroll position.
    Re-uses the same enrichment the page route computes so the response
    shape is identical to what the template received on first paint.
    """
    try:
        from kroger_mcp.tools.recipe_tools import _find_recipe
        from kroger_mcp.web.routes.recipes import enrich_ingredients_for_view

        recipe = _find_recipe(recipe_id)
        if not recipe:
            return JSONResponse(
                status_code=404,
                content={"error": f"Recipe '{recipe_id}' not found"},
            )
        ingredients = recipe.get("ingredients", []) or []
        for ing in ingredients:
            ing.setdefault("product_id", None)
            ing.setdefault("override", False)
        enriched = enrich_ingredients_for_view(request, ingredients)
        return {
            "success": True,
            "recipe_id": recipe_id,
            "ingredients": enriched,
            "updated_at": recipe.get("updated_at"),
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/recipes/{recipe_id}/add-to-cart")
async def add_recipe_to_cart(recipe_id: str, body: AddToCartBody):
    """
    Preview or confirm adding a recipe's ingredients to the local cart.

    - confirm=false  →  returns a list of ingredients that would be added
    - confirm=true   →  adds each Kroger-linked ingredient to the local cart
    """
    try:
        from kroger_mcp.tools.recipe_tools import _find_recipe

        recipe = _find_recipe(recipe_id)
        if not recipe:
            return JSONResponse(
                status_code=404,
                content={"error": f"Recipe '{recipe_id}' not found"},
            )

        ingredients = recipe.get("ingredients", [])
        # Only ingredients with a real Kroger product_id (not overrides)
        cart_items = [
            ing for ing in ingredients if ing.get("product_id") and not ing.get("override")
        ]

        if not body.confirm:
            # Preview mode — return what would be added
            preview = [
                {
                    "product_id": ing.get("product_id"),
                    "name": ing.get("name"),
                    "quantity": ing.get("quantity") or 1,
                    "unit": ing.get("unit"),
                }
                for ing in cart_items
            ]
            override_items = [
                {"name": ing.get("name"), "reason": ing.get("override_reason")}
                for ing in ingredients
                if ing.get("override")
            ]
            return {
                "recipe_id": recipe_id,
                "recipe_name": recipe.get("name"),
                "items_to_add": preview,
                "manual_items": override_items,
                "modality": body.modality,
                "confirm": False,
            }

        # Confirm mode — add to local cart
        from kroger_mcp.tools.cart_tools import _add_item_to_local_cart

        added = []
        failed = []
        for ing in cart_items:
            try:
                qty = 1
                raw_qty = ing.get("quantity")
                if raw_qty is not None:
                    try:
                        qty = max(1, int(float(str(raw_qty))))
                    except (ValueError, TypeError):
                        qty = 1
                _add_item_to_local_cart(
                    product_id=ing["product_id"],
                    quantity=qty,
                    modality=body.modality,
                    product_details={
                        "description": ing.get("name"),
                        "brand": ing.get("brand"),
                    },
                )
                added.append(ing.get("name"))
            except Exception as exc:
                failed.append({"name": ing.get("name"), "error": str(exc)})

        return {
            "success": True,
            "recipe_id": recipe_id,
            "recipe_name": recipe.get("name"),
            "added": added,
            "failed": failed,
            "modality": body.modality,
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
