"""Shopping list API endpoints."""
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.tools.shopping_list_tools import (
    _load_shopping_list,
    _save_shopping_list,
    _generate_list_item_id,
    _consolidate_items,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/shopping-list
# ---------------------------------------------------------------------------

@router.get('/api/shopping-list')
async def get_shopping_list():
    """Return the current shopping list JSON."""
    try:
        data = _load_shopping_list()
        return JSONResponse(content=data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to load shopping list: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# POST /api/shopping-list/add-recipe
# ---------------------------------------------------------------------------

class AddRecipeBody(BaseModel):
    recipe_id: str
    servings_override: Optional[int] = None


@router.post('/api/shopping-list/add-recipe')
async def add_recipe_to_list(body: AddRecipeBody):
    """
    Add a recipe's ingredients to the shopping list, scaled to the requested
    servings.  The session pantry-attention gate is intentionally bypassed for
    the web UI.
    """
    try:
        from kroger_mcp.tools.recipe_tools import _find_recipe
        from kroger_mcp.tools.shared import get_default_servings

        recipe = _find_recipe(body.recipe_id)
        if not recipe:
            return JSONResponse(
                status_code=404,
                content={"error": f"Recipe '{body.recipe_id}' not found"},
            )

        household_default = get_default_servings()
        servings = body.servings_override if body.servings_override else household_default
        recipe_base = recipe.get("servings", 4)
        scale_factor = servings / recipe_base

        # Optional pantry context for intelligent skipping
        pantry_context: dict = {}
        try:
            from kroger_mcp.analytics.pantry import get_pantry_status
            for item in get_pantry_status(apply_depletion=True):
                pantry_context[item['product_id']] = item.get("level_percent", 0)
        except Exception:
            pass

        data = _load_shopping_list()
        items_added = 0
        items_skipped = 0

        for ing in recipe.get("ingredients", []):
            name = ing.get("name", "Unknown")
            qty = ing.get("quantity", 1)
            unit = ing.get("unit", "")
            product_id = ing.get("product_id")
            is_override = ing.get("override", False)

            try:
                qty_num = float(qty) if qty not in (None, '', 0) else 1.0
            except (ValueError, TypeError):
                qty_num = 1.0
            raw_scaled = qty_num * scale_factor
            # Smart rounding: produce realistic cooking quantities
            # Quantities >= 3 round to nearest whole number
            # Quantities >= 1 round to nearest 0.5
            # Quantities < 1 round to nearest 0.25 (quarter measures)
            if raw_scaled >= 3:
                scaled_qty = round(raw_scaled)
            elif raw_scaled >= 1:
                scaled_qty = round(raw_scaled * 2) / 2  # nearest 0.5
            elif raw_scaled > 0:
                scaled_qty = max(0.25, round(raw_scaled * 4) / 4)  # nearest 0.25
            else:
                scaled_qty = 1

            if is_override:
                override_reason = ing.get("override_reason", "Not from Kroger")
                data["items"].append({
                    "id": _generate_list_item_id(),
                    "product_id": None,
                    "ingredient_name": name,
                    # Web-facing alias so the template can use item.name
                    "name": name,
                    "quantity": scaled_qty,
                    "unit": unit,
                    "sources": [{
                        "recipe_id": body.recipe_id,
                        "recipe_name": recipe.get("name"),
                        "servings_used": servings,
                        "original_quantity": qty,
                        "scaled_quantity": scaled_qty,
                    }],
                    "added_at": datetime.now().isoformat(),
                    "notes": f"Manual: {override_reason}",
                    "manual_purchase": True,
                    "recipe_name": recipe.get("name"),
                })
                items_added += 1
                continue

            # Skip if pantry level is adequate
            if product_id and pantry_context.get(product_id, 0) >= 30:
                items_skipped += 1
                continue

            data["items"].append({
                "id": _generate_list_item_id(),
                "product_id": product_id,
                "ingredient_name": name,
                "name": name,
                "quantity": scaled_qty,
                "unit": unit,
                "sources": [{
                    "recipe_id": body.recipe_id,
                    "recipe_name": recipe.get("name"),
                    "servings_used": servings,
                    "original_quantity": qty,
                    "scaled_quantity": scaled_qty,
                }],
                "added_at": datetime.now().isoformat(),
                "notes": None,
                "recipe_name": recipe.get("name"),
            })
            items_added += 1

        data["items"] = _consolidate_items(data["items"])
        _save_shopping_list(data)

        return JSONResponse(content={
            "success": True,
            "recipe_name": recipe.get("name"),
            "items_added": items_added,
            "items_skipped": items_skipped,
            "total_items": len(data["items"]),
            "message": (
                f"Added {items_added} ingredients from '{recipe.get('name')}' "
                f"(scaled to {servings} servings)"
                + (f". {items_skipped} item(s) skipped (well-stocked pantry)." if items_skipped else "")
            ),
        })

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to add recipe: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# DELETE /api/shopping-list/{item_id}
# ---------------------------------------------------------------------------

@router.delete('/api/shopping-list/{item_id}')
async def remove_shopping_list_item(item_id: str):
    """Remove a single item from the shopping list by its id."""
    try:
        data = _load_shopping_list()
        original_count = len(data["items"])
        data["items"] = [
            item for item in data["items"]
            if item.get("id") != item_id and item.get("product_id") != item_id
        ]
        removed = original_count - len(data["items"])
        if removed == 0:
            return JSONResponse(
                status_code=404,
                content={"error": f"Item '{item_id}' not found"},
            )
        _save_shopping_list(data)
        return JSONResponse(content={
            "success": True,
            "removed": item_id,
            "remaining": len(data["items"]),
        })
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to remove item: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# PATCH /api/shopping-list/{item_id}
# ---------------------------------------------------------------------------

class UpdateItemBody(BaseModel):
    quantity: Optional[float] = None
    notes: Optional[str] = None


@router.patch('/api/shopping-list/{item_id}')
async def update_shopping_list_item(item_id: str, body: UpdateItemBody):
    """Update quantity or notes for a shopping list item."""
    try:
        data = _load_shopping_list()
        found = False
        for item in data["items"]:
            if item.get("id") == item_id:
                found = True
                if body.quantity is not None:
                    item["quantity"] = body.quantity
                if body.notes is not None:
                    item["notes"] = body.notes
                item["last_updated"] = datetime.now().isoformat()
                break

        if not found:
            return JSONResponse(
                status_code=404,
                content={"error": f"Item '{item_id}' not found"},
            )

        _save_shopping_list(data)
        return JSONResponse(content={"success": True, "item_id": item_id})

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to update item: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# POST /api/shopping-list/add-to-cart
# ---------------------------------------------------------------------------

class AddToCartBody(BaseModel):
    confirm: bool = False
    modality: str = "PICKUP"


@router.post('/api/shopping-list/add-to-cart')
async def shopping_list_to_cart(body: AddToCartBody):
    """
    confirm=False → return preview of items to be added.
    confirm=True  → add items to Kroger cart, update local tracking, clear list.
    """
    try:
        data = _load_shopping_list()
        items = data.get("items", [])

        if not items:
            return JSONResponse(content={
                "success": True,
                "message": "Shopping list is empty",
                "items": [],
            })

        # Optional pantry context
        pantry_context: dict = {}
        try:
            from kroger_mcp.analytics.pantry import get_pantry_status
            for pi in get_pantry_status(apply_depletion=True):
                pantry_context[pi['product_id']] = pi.get("level_percent", 0)
        except Exception:
            pass

        items_to_add = []
        items_to_skip = []
        items_manual = []

        for item in items:
            product_id = item.get("product_id")
            name = item.get("name") or item.get("ingredient_name") or product_id

            if item.get("manual_purchase"):
                items_manual.append({
                    "product_id": None,
                    "name": name,
                    "quantity": item.get("quantity", 1),
                    "unit": item.get("unit", ""),
                    "notes": item.get("notes", "Manual purchase required"),
                })
                continue

            if not product_id:
                items_to_skip.append({
                    "name": name,
                    "reason": "No product ID — search for product first",
                })
                continue

            pantry_level = pantry_context.get(product_id)
            if pantry_level is not None and pantry_level >= 30:
                items_to_skip.append({
                    "product_id": product_id,
                    "name": name,
                    "reason": f"Pantry at {pantry_level}%",
                })
            else:
                items_to_add.append({
                    "product_id": product_id,
                    "name": name,
                    "quantity": max(1, round(item.get("quantity", 1))),
                    "recipe_name": item.get("recipe_name") or (
                        item.get("sources", [{}])[0].get("recipe_name") if item.get("sources") else None
                    ),
                })

        # Preview mode
        if not body.confirm:
            return JSONResponse(content={
                "success": True,
                "confirmation_required": True,
                "items": items_to_add,
                "items_to_skip": items_to_skip,
                "manual_purchase": items_manual,
                "summary": {
                    "to_add": len(items_to_add),
                    "to_skip": len(items_to_skip),
                    "manual": len(items_manual),
                },
            })

        # Confirm mode — add to Kroger cart
        if not items_to_add:
            return JSONResponse(content={
                "success": True,
                "message": "No purchasable items (all stocked or missing product IDs)",
                "items_added": 0,
            })

        from kroger_mcp.tools.shared import get_authenticated_client
        from kroger_mcp.tools.cart_tools import _add_item_to_local_cart

        client = await asyncio.to_thread(get_authenticated_client)
        api_items = [
            {"upc": it["product_id"], "quantity": it["quantity"], "modality": body.modality}
            for it in items_to_add
        ]

        # Add to Kroger cart with per-item fallback on 400
        failed_items = []
        added_items = list(items_to_add)
        try:
            await asyncio.to_thread(client.cart.add_to_cart, api_items)
        except Exception as batch_err:
            batch_err_str = str(batch_err)
            is_400 = "400" in batch_err_str or "Bad Request" in batch_err_str
            is_401 = "401" in batch_err_str or "Unauthorized" in batch_err_str
            if is_401:
                raise
            if is_400 and len(api_items) > 1:
                # Retry each item individually
                added_items = []
                for api_item, orig_item in zip(api_items, items_to_add):
                    try:
                        await asyncio.to_thread(client.cart.add_to_cart, [api_item])
                        added_items.append(orig_item)
                    except Exception:
                        failed_items.append(orig_item)
            else:
                raise

        added_ids = {it["product_id"] for it in added_items}

        for it in added_items:
            try:
                _add_item_to_local_cart(
                    product_id=it["product_id"],
                    quantity=it["quantity"],
                    modality=body.modality,
                )
            except Exception:
                pass

        # Clear successfully-added items from the list; keep manual and failed items
        data["items"] = [
            item for item in data["items"]
            if item.get("manual_purchase")
            or not item.get("product_id")
            or item.get("product_id") not in added_ids
        ]
        _save_shopping_list(data)

        result = {
            "success": True,
            "items_added": len(added_items),
            "items_skipped": len(items_to_skip),
            "manual_purchase": items_manual,
            "message": f"Added {len(added_items)} items to your Kroger cart.",
        }
        if failed_items:
            result["items_failed"] = len(failed_items)
            result["failed_items"] = [f["name"] for f in failed_items]
            result["warning"] = (
                f"{len(failed_items)} item(s) rejected by Kroger API "
                "(invalid product ID or not available at this location)"
            )
        return JSONResponse(content=result)

    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err or "Authentication" in err:
            return JSONResponse(
                status_code=401,
                content={"error": "Not authenticated. Please log in via Claude first."},
            )
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to send to cart: {err}"},
        )
