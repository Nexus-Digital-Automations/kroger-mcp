"""
Recipe-Pantry integration for smart shopping lists.

Matches recipe ingredients to pantry items and generates optimized shopping lists
that consider current inventory levels.
"""

from typing import Any

from .database import ensure_initialized
from .pantry import get_pantry_item, get_pantry_status


def match_ingredient_to_pantry(
    ingredient_name: str,
    product_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Find pantry item matching a recipe ingredient.

    Args:
        ingredient_name: Name of the ingredient
        product_id: Optional product ID if linked
        user_id: Owner whose pantry to search; None resolves to the default user.
            Must be threaded through or the match silently targets the default
            user's pantry — a multi-tenant correctness bug in the deduction path.

    Returns:
        Pantry item info or None if not found
    """
    # If we have a product_id, try direct match
    if product_id:
        pantry_item = get_pantry_item(product_id, user_id)
        if pantry_item:
            return pantry_item

    # Otherwise, try to match by description (fuzzy matching)
    pantry_items = get_pantry_status(apply_depletion=True, user_id=user_id)

    # Normalize ingredient name for matching
    ingredient_lower = ingredient_name.lower()
    ingredient_words = set(ingredient_lower.split())

    best_match = None
    best_score = 0

    for item in pantry_items:
        description = (item.get("description") or "").lower()
        if not description:
            continue

        # Simple word overlap scoring
        desc_words = set(description.split())
        overlap = len(ingredient_words & desc_words)

        # Boost for exact substring match
        if ingredient_lower in description or description in ingredient_lower:
            overlap += 2

        if overlap > best_score:
            best_score = overlap
            best_match = item

    # Require at least one word match
    if best_score > 0:
        return best_match

    return None


def check_recipe_pantry(
    recipe_id: str, scale: float = 1.0, low_threshold: int = 30
) -> dict[str, Any]:
    """
    Check pantry for recipe ingredients and categorize by availability.

    Args:
        recipe_id: Recipe identifier
        scale: Multiplier for recipe quantities
        low_threshold: Consider "have enough" if pantry level above this

    Returns:
        Dict with categorized ingredients:
        - have_enough: Items with sufficient pantry level
        - low_but_usable: Items low but might still work
        - need_to_buy: Items below threshold or not in pantry
        - unknown: Items not tracked in pantry
    """
    ensure_initialized()

    # Recipes are stored in the JSON recipe store (kroger_recipes.json), not the
    # analytics DB — load from the single source of truth used by the recipes tool.
    from kroger_mcp.tools.recipe_tools import _find_recipe

    recipe = _find_recipe(recipe_id)
    ingredients = (recipe or {}).get("ingredients") or []

    if not recipe or not ingredients:
        return {
            "success": False,
            "error": f"Recipe '{recipe_id}' not found or has no ingredients",
        }

    recipe_name = recipe.get("name", recipe_id)

    result: dict[str, Any] = {
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "scale": scale,
        "have_enough": [],
        "low_but_usable": [],
        "need_to_buy": [],
        "unknown": [],
    }

    for ing in ingredients:
        ing_name = ing.get("name", "")
        product_id = ing.get("product_id")
        is_optional = ing.get("is_optional", False)

        # Try to find in pantry
        pantry_item = match_ingredient_to_pantry(ing_name, product_id)

        item_info = {
            "ingredient": ing_name,
            "quantity": ing.get("quantity"),
            "unit": ing.get("unit"),
            "product_id": product_id,
            "is_optional": bool(is_optional),
        }

        if pantry_item:
            level = pantry_item.get("level_percent", 0)
            item_info["pantry_level"] = level
            item_info["pantry_description"] = pantry_item.get("description")
            item_info["days_until_empty"] = pantry_item.get("days_until_empty")

            if level >= low_threshold:
                result["have_enough"].append(item_info)
            elif level > 10:
                result["low_but_usable"].append(item_info)
            else:
                result["need_to_buy"].append(item_info)
        else:
            result["unknown"].append(item_info)

    # Summary
    result["summary"] = {
        "total_ingredients": len(ingredients),
        "have_enough_count": len(result["have_enough"]),
        "low_count": len(result["low_but_usable"]),
        "need_count": len(result["need_to_buy"]),
        "unknown_count": len(result["unknown"]),
        "ready_to_cook": len(result["need_to_buy"]) == 0 and len(result["unknown"]) == 0,
    }

    return result


def generate_shopping_list(
    recipe_ids: list[str],
    combine_duplicates: bool = True,
    skip_in_pantry: bool = True,
    pantry_threshold: int = 30,
    scale: float = 1.0,
) -> dict[str, Any]:
    """
    Generate optimized shopping list for multiple recipes.

    Args:
        recipe_ids: List of recipe identifiers
        combine_duplicates: Merge same ingredients across recipes
        skip_in_pantry: Skip items already in pantry above threshold
        pantry_threshold: Minimum pantry level to skip
        scale: Recipe quantity multiplier

    Returns:
        Shopping list with items to buy and optional items
    """
    ensure_initialized()

    # Recipes live in the JSON recipe store, not the analytics DB.
    from kroger_mcp.tools.recipe_tools import _find_recipe

    all_ingredients = []
    recipe_names: dict[str, str] = {}

    def _recipe_name_for(ing: dict[str, Any]) -> str | None:
        key = ing.get("from_recipe")
        if key is None:
            return None
        return recipe_names.get(str(key))

    # Gather ingredients from all recipes
    for recipe_id in recipe_ids:
        recipe = _find_recipe(recipe_id)
        if not recipe:
            continue
        recipe_names[recipe_id] = recipe.get("name", recipe_id)
        for ing in recipe.get("ingredients", []):
            all_ingredients.append({**ing, "from_recipe": recipe_id})

    if not all_ingredients:
        return {"success": False, "error": "No ingredients found for specified recipes"}

    # Group and optionally combine ingredients
    shopping_items: dict[str, dict[str, Any]] = {}
    optional_items: dict[str, dict[str, Any]] = {}
    skipped_items = []

    for ing in all_ingredients:
        ing_name = ing.get("name", "").lower()
        product_id = ing.get("product_id")
        is_optional = ing.get("is_optional", False)
        quantity = (ing.get("quantity") or 1) * scale
        unit = ing.get("unit", "")

        # Check pantry if skip_in_pantry is enabled
        if skip_in_pantry:
            pantry_item = match_ingredient_to_pantry(ing_name, product_id)
            if pantry_item and pantry_item.get("level_percent", 0) >= pantry_threshold:
                skipped_items.append(
                    {
                        "ingredient": ing.get("name"),
                        "pantry_level": pantry_item.get("level_percent"),
                        "from_recipe": _recipe_name_for(ing),
                    }
                )
                continue

        # Create key for combining
        key = product_id or ing_name

        target = optional_items if is_optional else shopping_items

        if combine_duplicates and key in target:
            # Combine quantities if same unit
            existing = target[key]
            if existing.get("unit") == unit:
                existing["quantity"] = (existing.get("quantity") or 0) + quantity
                existing["from_recipes"].append(_recipe_name_for(ing))
        else:
            target[key] = {
                "ingredient": ing.get("name"),
                "quantity": quantity,
                "unit": unit,
                "product_id": product_id,
                "product_description": ing.get("product_description"),
                "from_recipes": [_recipe_name_for(ing)],
            }

    return {
        "success": True,
        "recipes": list(recipe_names.values()),
        "scale": scale,
        "to_buy": list(shopping_items.values()),
        "optional": list(optional_items.values()),
        "skipped_in_pantry": skipped_items,
        "summary": {
            "items_to_buy": len(shopping_items),
            "optional_items": len(optional_items),
            "skipped_from_pantry": len(skipped_items),
        },
    }


def get_recipes_for_pantry() -> dict[str, Any]:
    """
    Find recipes that can be made with current pantry inventory.

    Returns:
        Dict with recipes sorted by feasibility
    """
    ensure_initialized()

    # Recipes live in the JSON recipe store, not the analytics DB.
    from kroger_mcp.tools.recipe_tools import _load_recipes

    data = _load_recipes()
    recipes = data.get("recipes", [])

    results = []
    for recipe in recipes:
        recipe_id = recipe.get("id")
        if not recipe_id:
            continue
        check = check_recipe_pantry(recipe_id)
        if check.get("summary"):
            summary = check["summary"]
            feasibility = summary["have_enough_count"] / max(1, summary["total_ingredients"])
            results.append(
                {
                    "recipe_id": recipe_id,
                    "recipe_name": recipe.get("name", recipe_id),
                    "feasibility": round(feasibility, 2),
                    "have_ingredients": summary["have_enough_count"],
                    "need_ingredients": summary["need_count"] + summary["unknown_count"],
                    "ready_to_cook": summary["ready_to_cook"],
                }
            )

    # Sort by feasibility (highest first)
    results.sort(key=lambda r: r["feasibility"], reverse=True)

    return {
        "recipes": results,
        "ready_to_cook": [r for r in results if r["ready_to_cook"]],
        "summary": {
            "total_recipes": len(results),
            "ready_count": len([r for r in results if r["ready_to_cook"]]),
        },
    }
