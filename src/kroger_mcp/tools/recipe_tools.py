"""
Recipe management and selective ordering tools.

Provides tools for:
- Saving and managing recipes with ingredient lists
- Ordering recipe ingredients with selective opt-out
- Preview orders before adding to cart
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from ._storage import JsonStore
from .shared import get_authenticated_client

logger = logging.getLogger(__name__)

# Recipe storage file
_BASE_DIR = Path(__file__).parent.parent.parent.parent  # → kroger-mcp/
RECIPES_FILE = str(_BASE_DIR / "kroger_recipes.json")

_recipes_store = JsonStore(RECIPES_FILE, default=lambda: {"recipes": [], "last_updated": None})


def _trigger_notion_sync(op: str, data) -> None:
    """Fire-and-forget Notion sync. Never raises."""
    try:
        from ..analytics.notion_sync import (
            _load_sync_state,
            delete_recipe_page,
            push_recipe,
        )

        state = _load_sync_state()
        if not state.get("database_id"):
            return  # Not configured yet
        api_key = os.getenv("NOTION_API_KEY")
        if not api_key:
            return
        database_id = state["database_id"]
        if op == "push" and data:
            push_recipe(data, api_key, database_id)
        elif op == "delete" and data:
            delete_recipe_page(data, api_key)
    except Exception:
        pass  # Never block recipe operations


def _remember_ingredient_link(raw_name: str, product_id: str) -> None:
    """Teach the chef's account link memory when an ingredient is linked.

    Fire-and-forget: the chef acts as ``mcp_user_id()`` (its bound account), so
    links made in chat feed the same per-account "your usuals" memory the web
    popup reads. Never blocks or fails the link operation.
    """
    try:
        from ..analytics.ingredient_links import record_link
        from ..auth.dependencies import mcp_user_id

        # raw_name doubles as the display description — it's the ingredient label
        # the chef linked, which is the most recognizable thing for "your usuals".
        record_link(mcp_user_id(), raw_name, product_id, raw_name)
    except Exception:
        logger.warning("ingredient_link.remember_failed product=%s", product_id, exc_info=True)


def _load_recipes() -> dict[str, Any]:
    return _recipes_store.load()


def _normalize_ingredients(ingredients: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize ingredient data to prevent boolean/type issues."""
    for ing in ingredients:
        qty = ing.get("quantity")
        if isinstance(qty, bool):
            ing["quantity"] = 1
    return ingredients


def _save_recipes(data: dict[str, Any]) -> None:
    data["last_updated"] = datetime.now().isoformat()
    try:
        _recipes_store.save(data)
    except OSError as exc:
        logger.warning("Could not save recipes: %s", exc)


def _find_recipe(recipe_id: str) -> dict[str, Any] | None:
    """Find a recipe by ID."""
    data = _load_recipes()
    for recipe in data.get("recipes", []):
        if recipe.get("id") == recipe_id:
            return recipe
    return None


def _ingredient_matches(ingredient_name: str, skip_items: list[str]) -> bool:
    """Check if ingredient matches any skip item (case-insensitive, partial)."""
    if not skip_items:
        return False
    ingredient_lower = ingredient_name.lower()
    for skip in skip_items:
        skip_lower = skip.lower()
        if skip_lower in ingredient_lower or ingredient_lower in skip_lower:
            return True
    return False


def _validate_ingredients(ingredients):
    """Return list of validation errors for ingredient list."""
    errors = []
    for i, ing in enumerate(ingredients):
        if not ing.get("name"):
            errors.append(f"Ingredient {i+1}: missing 'name'")
            continue
        has_pid = bool(ing.get("product_id"))
        has_override = ing.get("override", False)
        if not has_pid and not has_override:
            errors.append(
                f"Ingredient {i+1} ('{ing.get('name')}'): requires a Kroger product_id. "
                f"Search with products(action='search') then link with recipes(action='link_ingredient'). "
                f"If not sold at Kroger, set override=True with override_reason."
            )
        elif has_override and not ing.get("override_reason"):
            errors.append(
                f"Ingredient {i+1} ('{ing.get('name')}'): override=True requires override_reason."
            )
    return errors


def register_tools(mcp):
    """Register recipe-related tools with the FastMCP server."""

    @mcp.tool()
    async def recipes(
        action: Literal[
            "save",
            "list",
            "get",
            "update",
            "delete",
            "search",
            "preview_order",
            "link_ingredient",
            "add_to_cart",
            "analyze",
        ] = Field(
            description=(
                "save — ingredients need product_id or override=True. "
                "link_ingredient — batch via links=[{recipe_id, ingredient_index, product_id}]. "
                "preview_order — check pantry before ordering. "
                "add_to_cart — order recipe ingredients. "
                "Other: list|get|update|delete|search|analyze"
            )
        ),
        recipe_id: str | None = Field(
            default=None,
            description="Recipe ID",
        ),
        name: str | None = Field(
            default=None,
            description="Recipe name",
        ),
        ingredients: list[dict[str, Any]] | None = Field(
            default=None,
            description="List of {name, quantity, unit, product_id (required), category, override (bool), override_reason (required if override=True)}",
        ),
        instructions: str | None = Field(
            default=None,
            description="Cooking instructions",
        ),
        servings: int | None = Field(
            default=None,
            description="Number of servings",
        ),
        description: str | None = Field(
            default=None,
            description="Brief recipe description",
        ),
        source: str | None = Field(
            default=None,
            description="Recipe source",
        ),
        tags: list[str] | None = Field(
            default=None,
            description="Tags for categorization",
        ),
        limit: int | None = Field(
            default=20,
            description="Max recipes to return",
        ),
        tag_filter: str | None = Field(
            default=None,
            description="Filter by tag",
        ),
        query: str | None = Field(
            default=None,
            description="Search term",
        ),
        skip_items: list[str] | None = Field(
            default=None,
            description="Ingredient names to skip",
        ),
        scale: float | None = Field(
            default=None,
            description="Scale factor e.g. 2.0 doubles recipe",
        ),
        ingredient_index: int | None = Field(
            default=None,
            description="Ingredient index 0-based",
        ),
        product_id: str | None = Field(
            default=None,
            description="Kroger product ID to link",
        ),
        links: list[dict[str, Any]] | None = Field(
            default=None,
            description="Batch links: [{recipe_id, ingredient_index, product_id}]",
        ),
        modality: str | None = Field(
            default=None,
            description="PICKUP or DELIVERY",
        ),
        confirm: bool | None = Field(
            default=None,
            description="True to confirm add after preview",
        ),
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Recipe management with Kroger product linking.

        CRITICAL: Every ingredient requires either a product_id (from products tool)
        or override=True + override_reason (for items not at Kroger).

        Workflow: save recipe → link_ingredient (batch: links=[...]) → preview_order → add_to_cart.
        analyze — health score and cost estimate.
        Auto-syncs to Notion if configured.
        """
        return await asyncio.to_thread(
            _recipes_impl,
            action,
            recipe_id,
            name,
            ingredients,
            instructions,
            servings,
            description,
            source,
            tags,
            limit,
            tag_filter,
            query,
            skip_items,
            scale,
            ingredient_index,
            product_id,
            links,
            modality,
            confirm,
            ctx,
        )

    def _recipes_impl(
        action,
        recipe_id,
        name,
        ingredients,
        instructions,
        servings,
        description,
        source,
        tags,
        limit,
        tag_filter,
        query,
        skip_items,
        scale,
        ingredient_index,
        product_id,
        links,
        modality,
        confirm,
        ctx,
    ):
        match action:
            case "save":
                if not name:
                    return {"success": False, "error": "name is required"}
                if not ingredients:
                    return {"success": False, "error": "At least one ingredient is required"}

                validation_errors = _validate_ingredients(ingredients)
                if validation_errors:
                    return {
                        "success": False,
                        "error": "Recipe ingredients require Kroger product IDs",
                        "validation_errors": validation_errors,
                        "tip": (
                            "Search for each ingredient with products(action='search'), then include "
                            "product_id when saving. For items not sold at Kroger, set override=True "
                            "and provide override_reason."
                        ),
                    }

                if instructions:
                    instructions = instructions.replace("\\n", "\n")
                recipe_id_new = str(uuid.uuid4())[:8]
                recipe = {
                    "id": recipe_id_new,
                    "name": name,
                    "description": description,
                    "servings": servings if servings is not None else 4,
                    "ingredients": _normalize_ingredients(ingredients),
                    "instructions": instructions,
                    "source": source if source is not None else "user provided",
                    "tags": tags or [],
                    "created_at": datetime.now().isoformat(),
                    "last_ordered_at": None,
                    "times_ordered": 0,
                }

                data = _load_recipes()
                data["recipes"].append(recipe)
                _save_recipes(data)
                _trigger_notion_sync("push", recipe)

                if ctx:
                    ctx.info(f"Saved recipe '{name}' with {len(ingredients)} ingredients")

                return {
                    "success": True,
                    "recipe_id": recipe_id_new,
                    "message": f"Recipe '{name}' saved successfully",
                    "ingredient_count": len(ingredients),
                    "servings": servings if servings is not None else 4,
                }

            case "list":
                try:
                    data = _load_recipes()
                    recipe_list = data.get("recipes", [])

                    if tag_filter:
                        tag_lower = tag_filter.lower()
                        recipe_list = [
                            r
                            for r in recipe_list
                            if any(tag_lower in t.lower() for t in r.get("tags", []))
                        ]

                    recipe_list = sorted(
                        recipe_list,
                        key=lambda r: r.get("created_at", ""),
                        reverse=True,
                    )[: (limit or 20)]

                    summaries = []
                    for r in recipe_list:
                        summary = {
                            "id": r["id"],
                            "name": r["name"],
                            "description": r.get("description"),
                            "servings": r.get("servings"),
                            "ingredient_count": len(r.get("ingredients", [])),
                            "tags": r.get("tags", []),
                            "times_ordered": r.get("times_ordered", 0),
                            "created_at": r.get("created_at"),
                        }
                        try:
                            from ..analytics.recipe_scoring import calculate_health_score

                            hs = calculate_health_score(r, names_only=True)
                            summary["health_score"] = hs["score"]
                            summary["health_grade"] = hs["grade"]
                        except Exception:
                            pass
                        summaries.append(summary)

                    return {
                        "success": True,
                        "recipes": summaries,
                        "count": len(summaries),
                        "total_saved": len(data.get("recipes", [])),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get recipes: {str(e)}"}

            case "get":
                if not recipe_id:
                    return {"success": False, "error": "recipe_id is required"}
                try:
                    recipe = _find_recipe(recipe_id)
                    if not recipe:
                        return {"success": False, "error": f"Recipe '{recipe_id}' not found"}
                    try:
                        from ..analytics.recipe_scoring import (
                            calculate_health_score,
                            estimate_recipe_cost,
                        )
                        from .shared import get_preferred_location_id

                        loc_id = get_preferred_location_id()
                        recipe["health_score"] = calculate_health_score(recipe)
                        recipe["cost_estimate"] = estimate_recipe_cost(recipe, location_id=loc_id)
                    except Exception:
                        pass  # Never block get on scoring errors
                    return {"success": True, "recipe": recipe}
                except Exception as e:
                    return {"success": False, "error": f"Failed to get recipe: {str(e)}"}

            case "update":
                if not recipe_id:
                    return {"success": False, "error": "recipe_id is required"}
                try:
                    data = _load_recipes()
                    if ingredients is not None:
                        validation_errors = _validate_ingredients(ingredients)
                        if validation_errors:
                            return {
                                "success": False,
                                "error": "Recipe ingredients require Kroger product IDs",
                                "validation_errors": validation_errors,
                            }

                    found = False
                    for recipe in data.get("recipes", []):
                        if recipe.get("id") == recipe_id:
                            found = True
                            if name is not None:
                                recipe["name"] = name
                            if ingredients is not None:
                                recipe["ingredients"] = _normalize_ingredients(ingredients)
                            if instructions is not None:
                                recipe["instructions"] = instructions.replace("\\n", "\n")
                            if servings is not None:
                                recipe["servings"] = servings
                            if description is not None:
                                recipe["description"] = description
                            if tags is not None:
                                recipe["tags"] = tags
                            recipe["updated_at"] = datetime.now().isoformat()
                            break

                    if not found:
                        return {"success": False, "error": f"Recipe '{recipe_id}' not found"}

                    _save_recipes(data)
                    _trigger_notion_sync("push", _find_recipe(recipe_id))

                    return {
                        "success": True,
                        "message": f"Recipe '{recipe_id}' updated",
                        "recipe_id": recipe_id,
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to update recipe: {str(e)}"}

            case "delete":
                if not recipe_id:
                    return {"success": False, "error": "recipe_id is required"}
                try:
                    data = _load_recipes()
                    original_count = len(data.get("recipes", []))
                    data["recipes"] = [
                        r for r in data.get("recipes", []) if r.get("id") != recipe_id
                    ]
                    if len(data["recipes"]) == original_count:
                        return {"success": False, "error": f"Recipe '{recipe_id}' not found"}
                    _save_recipes(data)
                    _trigger_notion_sync("delete", recipe_id)
                    return {
                        "success": True,
                        "message": f"Recipe '{recipe_id}' deleted",
                        "remaining_recipes": len(data["recipes"]),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to delete recipe: {str(e)}"}

            case "search":
                if not query:
                    return {"success": False, "error": "query is required"}
                try:
                    data = _load_recipes()
                    query_lower = query.lower()
                    matches = []
                    for recipe in data.get("recipes", []):
                        if query_lower in recipe.get("name", "").lower():
                            matches.append(recipe)
                            continue
                        if any(query_lower in tag.lower() for tag in recipe.get("tags", [])):
                            matches.append(recipe)
                            continue
                        if query_lower in (recipe.get("description") or "").lower():
                            matches.append(recipe)

                    summaries = [
                        {
                            "id": r["id"],
                            "name": r["name"],
                            "description": r.get("description"),
                            "tags": r.get("tags", []),
                            "ingredient_count": len(r.get("ingredients", [])),
                        }
                        for r in matches
                    ]

                    return {
                        "success": True,
                        "query": query,
                        "matches": summaries,
                        "count": len(summaries),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to search: {str(e)}"}

            case "preview_order":
                if not recipe_id:
                    return {"success": False, "error": "recipe_id is required"}
                try:
                    recipe = _find_recipe(recipe_id)
                    if not recipe:
                        return {"success": False, "error": f"Recipe '{recipe_id}' not found"}

                    _skip = skip_items or []
                    _scale = scale if scale is not None else 1.0
                    ingredients_preview = []
                    manual_purchase = []
                    items_to_order = 0
                    items_to_skip_count = 0

                    for i, ing in enumerate(recipe.get("ingredients", [])):
                        ing_name = ing.get("name", "Unknown")
                        qty = ing.get("quantity", 1)
                        unit = ing.get("unit", "")
                        pid = ing.get("product_id")
                        is_override = ing.get("override", False)
                        will_skip = _ingredient_matches(ing_name, _skip)

                        if is_override:
                            action_val = "MANUAL"
                            override_reason = ing.get("override_reason")
                            manual_purchase.append(
                                {
                                    "index": i,
                                    "name": ing_name,
                                    "quantity": qty,
                                    "unit": unit,
                                    "scaled_quantity": round(qty * _scale, 2) if qty else None,
                                    "override_reason": override_reason,
                                }
                            )
                        elif will_skip:
                            action_val = "SKIP"
                            items_to_skip_count += 1
                        else:
                            action_val = "ORDER"
                            items_to_order += 1

                        ingredients_preview.append(
                            {
                                "index": i,
                                "name": ing_name,
                                "quantity": qty,
                                "unit": unit,
                                "scaled_quantity": round(qty * _scale, 2) if qty else None,
                                "product_id": pid,
                                "has_product_id": pid is not None,
                                "action": action_val,
                                "will_order": action_val == "ORDER",
                                "skip_reason": (
                                    "user has item"
                                    if will_skip and not is_override
                                    else ("manual purchase" if is_override else None)
                                ),
                                "override_reason": (
                                    ing.get("override_reason") if is_override else None
                                ),
                            }
                        )

                    return {
                        "success": True,
                        "recipe_id": recipe_id,
                        "recipe_name": recipe.get("name"),
                        "base_servings": recipe.get("servings", 4),
                        "scaled_servings": int(recipe.get("servings", 4) * _scale),
                        "scale": _scale,
                        "ingredients": ingredients_preview,
                        "items_to_order": items_to_order,
                        "items_to_skip": items_to_skip_count,
                        "manual_purchase": manual_purchase,
                        "total_ingredients": len(ingredients_preview),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to preview: {str(e)}"}

            case "link_ingredient":
                try:
                    if links:
                        if len(links) > 50:
                            return {
                                "success": False,
                                "error": "Maximum 50 links per batch request",
                            }

                        data = _load_recipes()
                        results = []
                        updated_recipes = set()

                        for link in links:
                            rid = link.get("recipe_id")
                            idx = link.get("ingredient_index")
                            pid = link.get("product_id")

                            if not all([rid, idx is not None, pid]):
                                results.append(
                                    {
                                        "success": False,
                                        "error": "Missing required fields",
                                        "link": link,
                                    }
                                )
                                continue

                            recipe = None
                            for r in data.get("recipes", []):
                                if r.get("id") == rid:
                                    recipe = r
                                    break

                            if not recipe:
                                results.append(
                                    {
                                        "success": False,
                                        "error": f"Recipe '{rid}' not found",
                                        "link": link,
                                    }
                                )
                                continue

                            recipe_ings = recipe.get("ingredients", [])
                            if idx < 0 or idx >= len(recipe_ings):
                                results.append(
                                    {
                                        "success": False,
                                        "error": f"Invalid ingredient index {idx}",
                                        "link": link,
                                    }
                                )
                                continue

                            recipe_ings[idx]["product_id"] = pid
                            recipe_ings[idx]["override"] = False
                            recipe_ings[idx]["override_reason"] = None
                            _remember_ingredient_link(recipe_ings[idx].get("name", ""), pid)
                            updated_recipes.add(rid)
                            results.append(
                                {
                                    "success": True,
                                    "recipe_id": rid,
                                    "ingredient_name": recipe_ings[idx].get("name"),
                                    "product_id": pid,
                                }
                            )

                        for r in data.get("recipes", []):
                            if r.get("id") in updated_recipes:
                                r["updated_at"] = datetime.now().isoformat()

                        _save_recipes(data)
                        success_count = sum(1 for r in results if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(links),
                                "successful": success_count,
                                "failed": len(links) - success_count,
                            },
                        }

                    if not all([recipe_id, ingredient_index is not None, product_id]):
                        return {
                            "success": False,
                            "error": (
                                "For single mode, provide recipe_id, ingredient_index, "
                                "and product_id. For batch mode, provide links list."
                            ),
                        }

                    data = _load_recipes()
                    for recipe in data.get("recipes", []):
                        if recipe.get("id") == recipe_id:
                            recipe_ings = recipe.get("ingredients", [])
                            if ingredient_index < 0 or ingredient_index >= len(recipe_ings):
                                return {
                                    "success": False,
                                    "error": f"Invalid ingredient index {ingredient_index}",
                                }
                            recipe_ings[ingredient_index]["product_id"] = product_id
                            recipe_ings[ingredient_index]["override"] = False
                            recipe_ings[ingredient_index]["override_reason"] = None
                            recipe["updated_at"] = datetime.now().isoformat()
                            _save_recipes(data)
                            _remember_ingredient_link(
                                recipe_ings[ingredient_index].get("name", ""), product_id
                            )
                            return {
                                "success": True,
                                "message": (
                                    f"Linked '{recipe_ings[ingredient_index]['name']}' "
                                    f"to product {product_id}"
                                ),
                                "ingredient": recipe_ings[ingredient_index],
                            }

                    return {"success": False, "error": f"Recipe '{recipe_id}' not found"}
                except Exception as e:
                    return {"success": False, "error": f"Failed to link: {str(e)}"}

            case "add_to_cart":
                if not recipe_id:
                    return {"success": False, "error": "recipe_id is required"}
                try:
                    recipe = _find_recipe(recipe_id)
                    if not recipe:
                        return {"success": False, "error": f"Recipe '{recipe_id}' not found"}

                    _skip = skip_items or []
                    _scale = scale if scale is not None else 1.0
                    _modality = modality if modality is not None else "PICKUP"
                    _confirm = confirm if confirm is not None else False

                    pantry_context = {}
                    try:
                        from ..analytics.pantry import get_pantry_status

                        pantry_items = get_pantry_status(apply_depletion=True)
                        for item in pantry_items:
                            pantry_context[item["product_id"]] = {
                                "level_percent": item.get("level_percent", 0),
                                "status": item.get("status"),
                                "days_until_empty": item.get("days_until_empty"),
                                "quantity_on_hand": item.get("quantity_on_hand"),
                                "unit": item.get("unit"),
                            }
                    except Exception:
                        pass

                    ingredients_preview = []
                    items_to_add = []
                    items_manual = []
                    items_to_skip = []
                    items_in_pantry = []
                    items_partial = []

                    for i, ing in enumerate(recipe.get("ingredients", [])):
                        ing_name = ing.get("name", "Unknown")
                        qty = ing.get("quantity", 1)
                        unit = ing.get("unit", "")
                        pid = ing.get("product_id")
                        is_override = ing.get("override", False)
                        scaled_qty = max(1, int(round(qty * _scale))) if qty else 1
                        user_skip = _ingredient_matches(ing_name, _skip)
                        pantry = pantry_context.get(pid, {}) if pid else {}
                        pantry_level = pantry.get("level_percent")
                        in_pantry = pantry_level is not None
                        on_hand = pantry.get("quantity_on_hand")
                        stored_unit = pantry.get("unit")
                        unit_match = (
                            stored_unit is not None and unit and stored_unit.lower() == unit.lower()
                        )
                        # Partial fulfillment is only safe when units match;
                        # otherwise we fall back to % heuristics.
                        on_hand_usable = (
                            float(on_hand) if (on_hand is not None and unit_match) else None
                        )

                        from_pantry_qty = 0
                        to_order_qty = scaled_qty
                        action_val = "ADD"
                        reason = ""

                        if is_override:
                            action_val = "MANUAL"
                            reason = (
                                f"Manual purchase: {ing.get('override_reason', 'Not from Kroger')}"
                            )
                            items_manual.append(
                                {
                                    "name": ing_name,
                                    "quantity": f"{scaled_qty} {unit}".strip(),
                                    "override_reason": ing.get(
                                        "override_reason", "Not from Kroger"
                                    ),
                                }
                            )
                        elif user_skip:
                            action_val = "SKIP"
                            reason = "User specified to skip"
                            items_to_skip.append(ing_name)
                        elif on_hand_usable is not None and on_hand_usable >= scaled_qty:
                            action_val = "SKIP"
                            from_pantry_qty = scaled_qty
                            to_order_qty = 0
                            reason = (
                                f"Pantry has {on_hand_usable:g} {stored_unit} on hand "
                                f"(need {scaled_qty})"
                            )
                            items_in_pantry.append(
                                {
                                    "name": ing_name,
                                    "pantry_level": pantry_level,
                                    "from_pantry": from_pantry_qty,
                                    "unit": stored_unit,
                                }
                            )
                            items_to_skip.append(ing_name)
                        elif on_hand_usable is not None and on_hand_usable > 0:
                            from_pantry_qty = int(on_hand_usable)
                            to_order_qty = max(1, scaled_qty - from_pantry_qty)
                            action_val = "PARTIAL"
                            reason = (
                                f"Use {from_pantry_qty} {stored_unit} from pantry, "
                                f"order {to_order_qty} more"
                            )
                            items_partial.append(
                                {
                                    "name": ing_name,
                                    "needed": scaled_qty,
                                    "from_pantry": from_pantry_qty,
                                    "to_order": to_order_qty,
                                    "unit": stored_unit,
                                }
                            )
                            if pid:
                                items_to_add.append(
                                    {
                                        "product_id": pid,
                                        "name": ing_name,
                                        "quantity": to_order_qty,
                                        "modality": _modality,
                                        "needed_quantity": scaled_qty,
                                        "from_pantry_quantity": from_pantry_qty,
                                        "unit": stored_unit,
                                        "recipe_id": recipe_id,
                                        "recipe_name": recipe.get("name"),
                                    }
                                )
                        elif in_pantry and pantry_level >= 30:
                            action_val = "SKIP"
                            from_pantry_qty = scaled_qty
                            to_order_qty = 0
                            reason = f"Pantry: {pantry_level}% remaining"
                            items_in_pantry.append({"name": ing_name, "pantry_level": pantry_level})
                            items_to_skip.append(ing_name)
                        else:
                            action_val = "ADD"
                            reason = (
                                "Not in pantry" if not in_pantry else f"Pantry low: {pantry_level}%"
                            )
                            if pid:
                                items_to_add.append(
                                    {
                                        "product_id": pid,
                                        "name": ing_name,
                                        "quantity": scaled_qty,
                                        "modality": _modality,
                                        "needed_quantity": scaled_qty,
                                        "from_pantry_quantity": 0,
                                        "unit": unit,
                                        "recipe_id": recipe_id,
                                        "recipe_name": recipe.get("name"),
                                    }
                                )

                        ingredients_preview.append(
                            {
                                "index": i,
                                "name": ing_name,
                                "quantity": f"{scaled_qty} {unit}".strip(),
                                "action": action_val,
                                "reason": reason,
                                "product_id": pid,
                                "pantry_level": pantry_level,
                                "quantity_on_hand": on_hand,
                                "pantry_unit": stored_unit,
                                "from_pantry": from_pantry_qty,
                                "to_order": to_order_qty,
                                "in_favorites": False,
                            }
                        )

                    if not _confirm:
                        return {
                            "success": True,
                            "confirmation_required": True,
                            "preview": {
                                "recipe_name": recipe.get("name"),
                                "servings": int(recipe.get("servings", 4) * _scale),
                                "scale": _scale,
                                "modality": _modality,
                                "ingredients": ingredients_preview,
                                "summary": {
                                    "items_to_add": len(items_to_add),
                                    "items_to_skip": len(items_to_skip),
                                    "items_in_pantry": len(items_in_pantry),
                                    "items_manual_purchase": len(items_manual),
                                    "items_partial_from_pantry": len(items_partial),
                                },
                                "partial_from_pantry": items_partial,
                            },
                            "items_in_pantry": items_in_pantry,
                            "manual_purchase_required": items_manual,
                            "next_step": (
                                "Review the ingredients above. "
                                "Call this tool again with confirm=True to add items to cart. "
                                "Use skip_items to exclude any additional items."
                                + (
                                    f" Note: {len(items_manual)} item(s) require manual purchase "
                                    f"(not available at Kroger)."
                                    if items_manual
                                    else ""
                                )
                            ),
                        }

                    if not items_to_add:
                        return {
                            "success": True,
                            "message": (
                                "No items to add - all ingredients are well-stocked, skipped, or manual"
                            ),
                            "items_ordered": [],
                            "items_skipped": items_to_skip,
                            "manual_purchase_required": items_manual,
                        }

                    if ctx:
                        ctx.info(f"Adding {len(items_to_add)} items to cart...")

                    client = get_authenticated_client()
                    api_items = [
                        {
                            "upc": item["product_id"],
                            "quantity": item["quantity"],
                            "modality": item["modality"],
                        }
                        for item in items_to_add
                    ]
                    client.cart.add_to_cart(api_items)

                    from .cart_tools import _add_item_to_local_cart

                    for item in items_to_add:
                        _add_item_to_local_cart(
                            item["product_id"], item["quantity"], item["modality"]
                        )

                    # Partial fulfillment leaves an implicit pantry draw —
                    # record it now so the user can confirm/deny at the pantry
                    # gap-reconciliation inbox before we touch on-hand counts.
                    try:
                        from ..analytics.pantry import create_pending_gap

                        for partial in items_partial:
                            matched_pid = next(
                                (
                                    it["product_id"]
                                    for it in items_to_add
                                    if it.get("name") == partial["name"]
                                ),
                                None,
                            )
                            if not matched_pid:
                                continue
                            create_pending_gap(
                                product_id=matched_pid,
                                needed_quantity=partial["needed"],
                                ordered_quantity=partial["to_order"],
                                unit=partial.get("unit"),
                                recipe_id=recipe_id,
                                recipe_name=recipe.get("name"),
                                product_description=partial["name"],
                            )
                    except Exception:
                        # Never let gap bookkeeping block the cart write.
                        pass

                    data = _load_recipes()
                    for r in data.get("recipes", []):
                        if r.get("id") == recipe_id:
                            r["times_ordered"] = r.get("times_ordered", 0) + 1
                            r["last_ordered_at"] = datetime.now().isoformat()
                            break
                    _save_recipes(data)

                    return {
                        "success": True,
                        "message": (
                            f"Added {len(items_to_add)} items to cart for '{recipe.get('name')}'"
                        ),
                        "items_ordered": [
                            {
                                "name": item["name"],
                                "quantity": item["quantity"],
                                "product_id": item["product_id"],
                            }
                            for item in items_to_add
                        ],
                        "items_skipped": items_to_skip,
                        "manual_purchase_required": items_manual,
                        "modality": _modality,
                        "reminder": (
                            "Please review your cart in the Kroger app before checkout. "
                            "Would you like to update any pantry levels?"
                            + (
                                f" Don't forget to source {len(items_manual)} item(s) manually."
                                if items_manual
                                else ""
                            )
                        ),
                    }

                except Exception as cart_error:
                    error_msg = str(cart_error)
                    if "401" in error_msg or "Unauthorized" in error_msg:
                        return {
                            "success": False,
                            "error": "Authentication failed. Run auth(action='force_reauth').",
                            "details": error_msg,
                        }
                    return {
                        "success": False,
                        "error": f"Failed to process recipe order: {error_msg}",
                    }

            case "analyze":
                if not recipe_id:
                    return {"success": False, "error": "recipe_id is required"}
                try:
                    recipe = _find_recipe(recipe_id)
                    if not recipe:
                        return {"success": False, "error": f"Recipe '{recipe_id}' not found"}

                    from ..analytics.recipe_scoring import (
                        calculate_health_score,
                        estimate_recipe_cost,
                        estimate_recipe_cost_with_api,
                    )
                    from .shared import get_client_credentials_client, get_preferred_location_id

                    loc_id = get_preferred_location_id()
                    health = calculate_health_score(recipe)

                    # Try API-backed cost, fall back to DB-only
                    api_fallback_note = None
                    try:
                        client = get_client_credentials_client()
                        cost = estimate_recipe_cost_with_api(recipe, loc_id, client)
                    except Exception as api_err:
                        cost = estimate_recipe_cost(recipe, location_id=loc_id)
                        api_fallback_note = f"API unavailable: {str(api_err)}"

                    if api_fallback_note:
                        cost["api_fallback_note"] = api_fallback_note

                    # Ingredient coverage report
                    ingredients = recipe.get("ingredients") or []
                    linked = [
                        {"index": i, "name": ing.get("name"), "product_id": ing["product_id"]}
                        for i, ing in enumerate(ingredients)
                        if ing.get("product_id")
                    ]
                    unlinked = [
                        {"index": i, "name": ing.get("name")}
                        for i, ing in enumerate(ingredients)
                        if not ing.get("product_id")
                    ]
                    coverage = {
                        "total_ingredients": len(ingredients),
                        "linked_count": len(linked),
                        "unlinked_count": len(unlinked),
                        "linked": linked,
                        "unlinked": unlinked,
                        "tip": (
                            (
                                "Use recipes(action='link_ingredient', recipe_id=..., "
                                "ingredient_index=..., product_id=...) to link unlinked ingredients "
                                "for better price and health data."
                            )
                            if unlinked
                            else None
                        ),
                    }

                    return {
                        "success": True,
                        "recipe_id": recipe_id,
                        "recipe_name": recipe.get("name"),
                        "health_score": health,
                        "cost_estimate": cost,
                        "ingredient_coverage": coverage,
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to analyze recipe: {str(e)}"}

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
