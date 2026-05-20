"""API routes for recipe write operations."""

import json
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


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


@router.put("/api/recipes/{recipe_id}/ingredients")
async def replace_recipe_ingredients(recipe_id: str, body: ReplaceIngredientsBody):
    """Replace the entire ingredient list for a recipe.

    Atomic alternative to the append-one POST endpoint. The recipe-detail
    inline editor uses this so add/edit/remove/reorder flow through a
    single round-trip. Safety scoring is re-derived at render time, so we
    do not persist it here.
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
        return {
            "success": True,
            "recipe_id": recipe_id,
            "ingredient_count": len(recipe["ingredients"]),
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.put("/api/recipes/{recipe_id}/instructions")
async def replace_recipe_instructions(recipe_id: str, body: ReplaceInstructionsBody):
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
        steps = [s.strip() for s in body.instructions if s and s.strip()]
        recipe["instructions"] = json.dumps(steps)
        recipe["updated_at"] = datetime.now().isoformat()
        _save_recipes(store)
        return {"success": True, "recipe_id": recipe_id, "step_count": len(steps)}
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
async def update_recipe(recipe_id: str, body: UpdateRecipeBody):
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
        return {"success": True, "recipe_id": recipe_id}
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
