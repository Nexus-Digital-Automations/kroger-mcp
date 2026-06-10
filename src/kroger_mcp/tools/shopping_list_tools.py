"""
Shopping list management tools.

Provides intermediate storage between recipes and cart:
- Add recipes to shopping list (auto-scaled to household servings)
- Consolidate quantities from multiple recipes
- Review before adding to cart
- Session requirement: Must call get_pantry_attention() first
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

logger = logging.getLogger(__name__)


def _resolve_shopping_user_id(user_id: str | None) -> str:
    """None falls back to mcp_user_id() (KROGER_MCP_USER_ID or default)."""
    from kroger_mcp.auth.dependencies import mcp_user_id

    return user_id if user_id is not None else mcp_user_id()


def _load_shopping_list(user_id: str | None = None) -> dict[str, Any]:
    """Return this user's shopping list in the legacy `{items, last_updated}` shape."""
    from kroger_mcp.analytics.database import get_db_connection

    owner = _resolve_shopping_user_id(user_id)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, product_id, name, quantity, unit, category,
                   purchased, recipe_source, added_at
            FROM user_shopping_lists WHERE user_id = ?
            ORDER BY added_at
            """,
            (owner,),
        ).fetchall()
        items = [
            {
                "id": row["id"],
                "product_id": row["product_id"],
                "name": row["name"],
                "quantity": row["quantity"],
                "unit": row["unit"],
                "category": row["category"],
                "purchased": bool(row["purchased"]),
                "recipe_source": row["recipe_source"],
                "added_at": row["added_at"],
            }
            for row in rows
        ]
        last_updated = rows[-1]["added_at"] if rows else None
        return {"items": items, "last_updated": last_updated}
    finally:
        conn.close()


def _save_shopping_list(data: dict[str, Any], user_id: str | None = None) -> None:
    """Replace this user's shopping list with data['items']."""
    from kroger_mcp.analytics.database import get_db_connection

    owner = _resolve_shopping_user_id(user_id)
    items = data.get("items", []) or []
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM user_shopping_lists WHERE user_id = ?", (owner,))
        for item in items:
            item_id = item.get("id") or _generate_list_item_id()
            conn.execute(
                """
                INSERT OR REPLACE INTO user_shopping_lists
                    (id, user_id, product_id, name, quantity, unit, category,
                     purchased, recipe_source, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    owner,
                    item.get("product_id"),
                    item.get("name", ""),
                    float(item.get("quantity", 1) or 1),
                    item.get("unit", ""),
                    item.get("category"),
                    1 if item.get("purchased") else 0,
                    item.get("recipe_source"),
                    item.get("added_at") or datetime.now().isoformat(),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _generate_list_item_id() -> str:
    """Generate unique ID for shopping list item."""
    return f"list_item_{str(uuid.uuid4())[:8]}"


def _consolidate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Consolidate shopping list items by product_id.

    Items with the same product_id have their quantities summed
    and sources combined.
    """
    consolidated = {}

    for item in items:
        product_id = item.get("product_id")
        if not product_id:
            # Items without product_id stay separate
            item_id = item.get("id", _generate_list_item_id())
            consolidated[item_id] = item
            continue

        if product_id in consolidated:
            # Consolidate quantities
            existing = consolidated[product_id]
            existing["quantity"] = existing.get("quantity", 0) + item.get("quantity", 0)

            # Merge sources
            existing_sources = existing.get("sources", [])
            new_sources = item.get("sources", [])
            existing["sources"] = existing_sources + new_sources

            # Update timestamp
            existing["last_updated"] = datetime.now().isoformat()
        else:
            # First occurrence of this product
            consolidated[product_id] = item.copy()

    return list(consolidated.values())


def _get_session_id(ctx: Context) -> str:
    """
    Extract session ID from MCP context.

    Falls back to 'default' if no context available (testing, etc.)
    """
    if ctx and hasattr(ctx, "session_id"):
        return str(ctx.session_id)
    return "default"


def _check_attention_requirement(ctx: Context) -> dict[str, Any] | None:
    """
    Check if get_pantry_attention was called this session.

    Returns error dict if not called, None if requirement met.
    """
    from ..config.session_state import get_session_manager

    session_id = _get_session_id(ctx)
    session_manager = get_session_manager()

    if not session_manager.was_tool_called(session_id, "get_pantry_attention"):
        return {
            "success": False,
            "error": "Session requirement not met",
            "error_code": "ATTENTION_REQUIRED",
            "message": (
                "You must call get_pantry_attention() before adding to shopping list. "
                "This ensures you review expiring items, low inventory, and what you "
                "already have before building your shopping list."
            ),
            "required_action": "Call get_pantry_attention() first",
        }

    return None  # Requirement met


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


def register_tools(mcp):
    """Register shopping list tools with the FastMCP server."""

    @mcp.tool()
    async def shopping_list(
        action: Literal["add_recipe", "get", "remove", "update_item", "add_to_cart"] = Field(
            description=(
                "add_recipe — adds recipe ingredients (auto-scaled, consolidated). "
                "Requires pantry(action='get_attention') first. "
                "add_to_cart — confirm=False previews, confirm=True executes. "
                "Other: get|remove|update_item"
            )
        ),
        recipe_id: str | None = Field(default=None, description="Recipe ID"),
        servings: int | None = Field(default=None, ge=1, le=20, description="Override servings"),
        skip_items: list[str] | None = Field(default=None, description="Ingredient names to skip"),
        item_id: str | None = Field(default=None, description="Item ID"),
        item_ids: list[str] | None = Field(default=None, description="Item IDs for batch remove"),
        clear_all: bool | None = Field(default=None, description="Clear entire list"),
        quantity: int | None = Field(default=None, ge=1, description="New quantity"),
        notes: str | None = Field(default=None, description="Item notes"),
        modality: str | None = Field(default=None, description="PICKUP or DELIVERY"),
        confirm: bool | None = Field(default=None, description="False=preview, True=execute"),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Shopping list — intermediate buffer between recipes and cart.

        SESSION REQUIREMENT: Call pantry(action='get_attention') before using.
        add_recipe — auto-scales to household servings, consolidates duplicates.
        add_to_cart — confirm=False to preview, confirm=True to execute.
        Supports skip_items to exclude specific ingredients.
        """
        return await asyncio.to_thread(
            _shopping_list_impl,
            action,
            recipe_id,
            servings,
            skip_items,
            item_id,
            item_ids,
            clear_all,
            quantity,
            notes,
            modality,
            confirm,
            ctx,
        )

    def _shopping_list_impl(
        action,
        recipe_id,
        servings,
        skip_items,
        item_id,
        item_ids,
        clear_all,
        quantity,
        notes,
        modality,
        confirm,
        ctx,
    ):
        match action:
            case "add_recipe":
                # Check session requirement
                requirement_error = _check_attention_requirement(ctx)
                if requirement_error:
                    return requirement_error

                try:
                    from .recipe_tools import _find_recipe
                    from .shared import get_default_servings

                    # Find recipe
                    recipe = _find_recipe(recipe_id)
                    if not recipe:
                        return {"success": False, "error": f"Recipe '{recipe_id}' not found"}

                    # Determine servings to use
                    household_default = get_default_servings()
                    if servings is None:
                        servings = household_default
                        using_default = True
                    else:
                        using_default = False

                    recipe_base_servings = recipe.get("servings", 4)
                    scale_factor = servings / recipe_base_servings

                    # Get pantry context
                    pantry_context = {}
                    try:
                        from ..analytics.pantry import get_pantry_status

                        pantry_items = get_pantry_status(apply_depletion=True)
                        for item in pantry_items:
                            pantry_context[item["product_id"]] = {
                                "level_percent": item.get("level_percent", 0),
                                "status": item.get("status"),
                            }
                    except Exception:
                        pass

                    # Load current shopping list
                    data = _load_shopping_list()
                    items_added = 0
                    items_skipped = 0
                    skip_reasons = {"pantry_threshold": [], "user_specified": []}

                    # Process ingredients
                    manual_purchase_items = []
                    for ing in recipe.get("ingredients", []):
                        name = ing.get("name", "Unknown")
                        quantity_val = ing.get("quantity", 1)
                        unit = ing.get("unit", "")
                        product_id = ing.get("product_id")
                        is_override = ing.get("override", False)

                        # Calculate scaled quantity
                        scaled_quantity = (
                            round(quantity_val * scale_factor, 2) if quantity_val else 1
                        )

                        # Handle override (manual purchase) items
                        if is_override:
                            override_reason = ing.get("override_reason", "Not from Kroger")
                            list_item = {
                                "id": _generate_list_item_id(),
                                "product_id": None,
                                "ingredient_name": name,
                                "quantity": scaled_quantity,
                                "unit": unit,
                                "sources": [
                                    {
                                        "recipe_id": recipe_id,
                                        "recipe_name": recipe.get("name"),
                                        "servings_used": servings,
                                        "original_quantity": quantity_val,
                                        "scaled_quantity": scaled_quantity,
                                    }
                                ],
                                "added_at": datetime.now().isoformat(),
                                "notes": f"Manual: {override_reason}",
                                "manual_purchase": True,
                            }
                            data["items"].append(list_item)
                            manual_purchase_items.append(
                                {"name": name, "override_reason": override_reason}
                            )
                            items_added += 1
                            continue

                        # Check if should skip
                        user_skip = _ingredient_matches(name, skip_items or [])
                        if user_skip:
                            items_skipped += 1
                            skip_reasons["user_specified"].append(name)
                            continue

                        # Check pantry
                        pantry = pantry_context.get(product_id, {}) if product_id else {}
                        pantry_level = pantry.get("level_percent")
                        if pantry_level is not None and pantry_level >= 30:
                            items_skipped += 1
                            skip_reasons["pantry_threshold"].append(
                                f"{name} (pantry at {pantry_level}%)"
                            )
                            continue

                        # Add to shopping list
                        list_item = {
                            "id": _generate_list_item_id(),
                            "product_id": product_id,
                            "ingredient_name": name,
                            "quantity": scaled_quantity,
                            "unit": unit,
                            "sources": [
                                {
                                    "recipe_id": recipe_id,
                                    "recipe_name": recipe.get("name"),
                                    "servings_used": servings,
                                    "original_quantity": quantity_val,
                                    "scaled_quantity": scaled_quantity,
                                }
                            ],
                            "added_at": datetime.now().isoformat(),
                            "notes": None,
                        }

                        data["items"].append(list_item)
                        items_added += 1

                    # Consolidate items
                    data["items"] = _consolidate_items(data["items"])
                    _save_shopping_list(data)

                    if ctx:
                        ctx.info(
                            f"Added {items_added} ingredients from '{recipe.get('name')}' to shopping list"
                        )

                    return {
                        "success": True,
                        "recipe_id": recipe_id,
                        "recipe_name": recipe.get("name"),
                        "recipe_base_servings": recipe_base_servings,
                        "servings_used": servings,
                        "household_default": household_default,
                        "using_household_default": using_default,
                        "scale_factor": scale_factor,
                        "items_added": items_added,
                        "items_skipped": items_skipped,
                        "skip_reasons": skip_reasons,
                        "manual_purchase_required": manual_purchase_items,
                        "shopping_list_total_items": len(data["items"]),
                        "message": (
                            f"Added {items_added} ingredients from '{recipe.get('name')}' "
                            f"(scaled to {servings} servings)"
                            + (
                                f". {len(manual_purchase_items)} item(s) require manual purchase."
                                if manual_purchase_items
                                else ""
                            )
                        ),
                    }

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to add recipe to shopping list: {str(e)}",
                    }

            case "get":
                try:
                    from .shared import get_default_servings

                    data = _load_shopping_list()
                    items = data.get("items", [])

                    # Extract recipes included
                    recipes_map = {}
                    for item in items:
                        for source in item.get("sources", []):
                            rid = source.get("recipe_id")
                            if rid and rid not in recipes_map:
                                recipes_map[rid] = {
                                    "recipe_id": rid,
                                    "recipe_name": source.get("recipe_name"),
                                    "servings": source.get("servings_used"),
                                }

                    recipes_included = list(recipes_map.values())
                    total_servings = sum(r["servings"] for r in recipes_included)

                    return {
                        "success": True,
                        "items": items,
                        "total_items": len(items),
                        "recipes_included": recipes_included,
                        "servings_summary": {
                            "household_default": get_default_servings(),
                            "total_servings_planned": total_servings,
                            "total_meals": len(recipes_included),
                        },
                    }

                except Exception as e:
                    return {"success": False, "error": f"Failed to get shopping list: {str(e)}"}

            case "remove":
                try:
                    data = _load_shopping_list()

                    if clear_all:
                        item_count = len(data["items"])
                        data["items"] = []
                        _save_shopping_list(data)
                        return {
                            "success": True,
                            "message": f"Cleared {item_count} items from shopping list",
                            "items_removed": item_count,
                        }

                    if item_id:
                        ids_to_remove = [item_id]
                    elif item_ids:
                        ids_to_remove = item_ids
                    else:
                        return {
                            "success": False,
                            "error": "Provide item_id, item_ids, or set clear_all=True",
                        }

                    original_count = len(data["items"])
                    data["items"] = [
                        item for item in data["items"] if item.get("id") not in ids_to_remove
                    ]
                    removed_count = original_count - len(data["items"])

                    _save_shopping_list(data)

                    return {
                        "success": True,
                        "items_removed": removed_count,
                        "remaining_items": len(data["items"]),
                        "message": f"Removed {removed_count} item(s) from shopping list",
                    }

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to remove from shopping list: {str(e)}",
                    }

            case "update_item":
                try:
                    data = _load_shopping_list()
                    found = False

                    for item in data["items"]:
                        if item.get("id") == item_id:
                            found = True
                            if quantity is not None:
                                item["quantity"] = quantity
                            if notes is not None:
                                item["notes"] = notes
                            item["last_updated"] = datetime.now().isoformat()
                            break

                    if not found:
                        return {
                            "success": False,
                            "error": f"Item '{item_id}' not found in shopping list",
                        }

                    _save_shopping_list(data)

                    return {
                        "success": True,
                        "message": f"Updated item '{item_id}'",
                        "item_id": item_id,
                    }

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to update shopping list item: {str(e)}",
                    }

            case "add_to_cart":
                # Check session requirement
                requirement_error = _check_attention_requirement(ctx)
                if requirement_error:
                    return requirement_error

                try:
                    from .cart_tools import _add_item_to_local_cart
                    from .shared import get_authenticated_client

                    data = _load_shopping_list()
                    items = data.get("items", [])

                    if not items:
                        return {
                            "success": True,
                            "message": "Shopping list is empty - nothing to add",
                            "items_added": 0,
                        }

                    # Get pantry context for re-check
                    pantry_context = {}
                    try:
                        from ..analytics.pantry import get_pantry_status

                        pantry_items = get_pantry_status(apply_depletion=True)
                        for item in pantry_items:
                            pantry_context[item["product_id"]] = {
                                "level_percent": item.get("level_percent", 0)
                            }
                    except Exception:
                        pass

                    # Build preview
                    items_to_add = []
                    items_to_skip = []
                    items_manual = []
                    _modality = modality or "PICKUP"
                    _confirm = confirm or False

                    for item in items:
                        product_id = item.get("product_id")
                        if item.get("manual_purchase"):
                            items_manual.append(
                                {
                                    "ingredient_name": item.get("ingredient_name"),
                                    "quantity": item.get("quantity"),
                                    "unit": item.get("unit", ""),
                                    "notes": item.get("notes", "Manual purchase required"),
                                }
                            )
                            continue
                        if not product_id:
                            items_to_skip.append(
                                {
                                    "ingredient_name": item.get("ingredient_name"),
                                    "reason": "No product_id (search for product first)",
                                }
                            )
                            continue

                        # Check pantry again
                        pantry = pantry_context.get(product_id, {})
                        pantry_level = pantry.get("level_percent")

                        if pantry_level is not None and pantry_level >= 30:
                            items_to_skip.append(
                                {
                                    "product_id": product_id,
                                    "ingredient_name": item.get("ingredient_name"),
                                    "reason": f"Pantry at {pantry_level}%",
                                    "action": "SKIP",
                                }
                            )
                        else:
                            from_recipes = [s.get("recipe_name") for s in item.get("sources", [])]
                            items_to_add.append(
                                {
                                    "product_id": product_id,
                                    "ingredient_name": item.get("ingredient_name"),
                                    "quantity": max(1, round(item.get("quantity", 1))),
                                    "from_recipes": from_recipes,
                                    "action": "ADD",
                                    "reason": (
                                        "Not in pantry"
                                        if pantry_level is None
                                        else f"Pantry low: {pantry_level}%"
                                    ),
                                }
                            )

                    # Preview mode
                    if not _confirm:
                        return {
                            "success": True,
                            "confirmation_required": True,
                            "preview": {
                                "items_to_add": len(items_to_add),
                                "items_to_skip": len(items_to_skip),
                                "items_manual_purchase": len(items_manual),
                                "modality": _modality,
                                "items": items_to_add + items_to_skip,
                                "manual_purchase_required": items_manual,
                            },
                            "next_step": (
                                "Review the items above. Call this tool again with confirm=True to add to cart."
                                + (
                                    f" Note: {len(items_manual)} item(s) require manual purchase."
                                    if items_manual
                                    else ""
                                )
                            ),
                        }

                    # Confirm mode - actually add to cart
                    if not items_to_add:
                        return {
                            "success": True,
                            "message": "No items to add - all are well-stocked, missing product_ids, or manual",
                            "items_added_to_cart": 0,
                            "items_skipped": len(items_to_skip),
                            "manual_purchase_required": items_manual,
                        }

                    if ctx:
                        ctx.info(f"Adding {len(items_to_add)} items from shopping list to cart...")

                    try:
                        client = get_authenticated_client()

                        # Format for Kroger API
                        api_items = [
                            {
                                "upc": item["product_id"],
                                "quantity": item["quantity"],
                                "modality": _modality,
                            }
                            for item in items_to_add
                        ]

                        client.cart.add_to_cart(api_items)

                        # Track in local cart
                        for item in items_to_add:
                            _add_item_to_local_cart(item["product_id"], item["quantity"], _modality)

                        # Clear shopping list
                        data["items"] = []
                        _save_shopping_list(data)

                        return {
                            "success": True,
                            "items_added_to_cart": len(items_to_add),
                            "items_skipped": len(items_to_skip),
                            "manual_purchase_required": items_manual,
                            "shopping_list_cleared": True,
                            "modality": _modality,
                            "message": f"Added {len(items_to_add)} items to cart. Shopping list has been cleared.",
                            "reminder": (
                                "Review your cart in the Kroger app before checkout."
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
                                "error": "Authentication failed. Run force_reauthenticate.",
                                "details": error_msg,
                            }
                        return {
                            "success": False,
                            "error": f"Failed to add to cart: {error_msg}",
                            "items_attempted": len(items_to_add),
                        }

                except Exception as e:
                    return {"success": False, "error": f"Failed to process shopping list: {str(e)}"}

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
