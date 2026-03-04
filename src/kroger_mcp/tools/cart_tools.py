"""
Cart tracking and management functionality
"""
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastmcp import Context
from pydantic import Field

from .shared import get_authenticated_client, get_preferred_location_id
from ..analytics.safety import (
    get_all_safe_product_ids,
    get_all_blocked_product_ids,
    get_disabled_ingredients,
    is_filtering_enabled,
)
from ..analytics.ingredients import check_product_safety
from ..analytics.deals import record_price_observation, calculate_cart_savings


# Cart storage file
CART_FILE = "kroger_cart.json"
ORDER_HISTORY_FILE = "kroger_order_history.json"


def _load_cart_data() -> Dict[str, Any]:
    """Load cart data from file"""
    try:
        if os.path.exists(CART_FILE):
            with open(CART_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {"current_cart": [], "last_updated": None, "preferred_location_id": None}


def _save_cart_data(cart_data: Dict[str, Any]) -> None:
    """Save cart data to file"""
    try:
        with open(CART_FILE, 'w') as f:
            json.dump(cart_data, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save cart data: {e}")


def _load_order_history() -> List[Dict[str, Any]]:
    """Load order history from file"""
    try:
        if os.path.exists(ORDER_HISTORY_FILE):
            with open(ORDER_HISTORY_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_order_history(history: List[Dict[str, Any]]) -> None:
    """Save order history to file"""
    try:
        with open(ORDER_HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save order history: {e}")


def _add_item_to_local_cart(
    product_id: str,
    quantity: int,
    modality: str,
    product_details: Dict[str, Any] = None,
) -> None:
    """Add an item to the local cart tracking and analytics database"""
    cart_data = _load_cart_data()
    current_cart = cart_data.get("current_cart", [])

    existing_item = None
    for item in current_cart:
        if item.get("product_id") == product_id and item.get("modality") == modality:
            existing_item = item
            break

    if existing_item:
        existing_item["quantity"] = existing_item.get("quantity", 0) + quantity
        existing_item["last_updated"] = datetime.now().isoformat()
    else:
        new_item = {
            "product_id": product_id,
            "quantity": quantity,
            "modality": modality,
            "added_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
        }
        if product_details:
            new_item.update(product_details)
        current_cart.append(new_item)

    cart_data["current_cart"] = current_cart
    cart_data["last_updated"] = datetime.now().isoformat()
    _save_cart_data(cart_data)

    try:
        from ..analytics.purchase_tracker import record_cart_add
        record_cart_add(product_id, quantity, modality, product_details)
    except Exception as e:
        print(f"Warning: Could not record analytics: {e}")

    if product_details:
        try:
            pricing = product_details.get("pricing", {})
            location_id = get_preferred_location_id()
            if pricing and location_id:
                record_price_observation(
                    product_id=product_id,
                    regular_price=pricing.get("regular_price"),
                    sale_price=pricing.get("sale_price") or pricing.get("price"),
                    location_id=location_id,
                    source="cart_add",
                )
        except Exception:
            pass

    try:
        from ..analytics.pantry import add_to_pantry
        add_to_pantry(product_id=product_id)
    except Exception:
        pass


def register_tools(mcp):
    """Register cart-related tools with the FastMCP server"""

    @mcp.tool()
    async def cart(
        action: Literal[
            "view",
            "add",
            "remove",
            "clear",
            "mark_placed",
            "view_history",
            "get_context",
        ] = Field(
            description=(
                "Action: 'view' - view current cart contents, "
                "'add' - add item(s) to cart (single: product_id+quantity+modality; batch: items list), "
                "'remove' - remove item from local cart tracking, "
                "'clear' - clear all items from local cart tracking, "
                "'mark_placed' - mark current cart as placed order and move to history, "
                "'view_history' - view history of placed orders, "
                "'get_context' - get pantry/favorites context for shopping decisions"
            )
        ),
        product_id: Optional[str] = Field(
            default=None,
            description="Product ID (for add single mode, remove)",
        ),
        quantity: Optional[int] = Field(
            default=1,
            description="Quantity to add, 1-99 (for add single mode)",
        ),
        modality: Optional[str] = Field(
            default="PICKUP",
            description=(
                "PICKUP or DELIVERY (for add single mode; "
                "for remove: filter by modality or None to remove all instances)"
            ),
        ),
        items: Optional[List[Dict[str, Any]]] = Field(
            default=None,
            description=(
                "Batch items for add: list of {product_id, quantity?, modality?, description?} (max 50)"
            ),
        ),
        preview_only: Optional[bool] = Field(
            default=False,
            description="Return preview without adding to cart (for add)",
        ),
        confirm_unsafe: Optional[bool] = Field(
            default=False,
            description="Override safety warnings and add flagged products (for add)",
        ),
        order_notes: Optional[str] = Field(
            default=None,
            description="Optional notes about the order (for mark_placed)",
        ),
        limit: Optional[int] = Field(
            default=10,
            description="Number of recent orders to show, 1-50 (for view_history)",
        ),
        product_ids: Optional[List[str]] = Field(
            default=None,
            description=(
                "Product IDs to check context for (for get_context); "
                "if None, returns all pantry/favorites context"
            ),
        ),
        pantry_threshold: Optional[int] = Field(
            default=30,
            description="Items above this pantry level % are suggested to skip (for get_context)",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Cart management operations."""
        match action:
            case "view":
                try:
                    cart_data = _load_cart_data()
                    current_cart = cart_data.get("current_cart", [])

                    total_quantity = sum(item.get("quantity", 0) for item in current_cart)
                    pickup_items = [i for i in current_cart if i.get("modality") == "PICKUP"]
                    delivery_items = [i for i in current_cart if i.get("modality") == "DELIVERY"]

                    savings_summary = None
                    try:
                        savings_summary = calculate_cart_savings(current_cart)
                    except Exception:
                        pass

                    result = {
                        "success": True,
                        "current_cart": current_cart,
                        "summary": {
                            "total_items": len(current_cart),
                            "total_quantity": total_quantity,
                            "pickup_items": len(pickup_items),
                            "delivery_items": len(delivery_items),
                            "last_updated": cart_data.get("last_updated"),
                        },
                    }

                    if savings_summary:
                        result["savings_summary"] = savings_summary

                    return result
                except Exception as e:
                    return {"success": False, "error": f"Failed to view cart: {str(e)}"}

            case "add":
                try:
                    if items is not None:
                        is_batch = True
                        if len(items) > 50:
                            return {"success": False, "error": "Maximum 50 items per batch request"}
                        formatted_items = [
                            {
                                "product_id": item["product_id"],
                                "quantity": item.get("quantity", 1),
                                "modality": item.get("modality", "PICKUP"),
                                "description": item.get("description"),
                            }
                            for item in items
                        ]
                    elif product_id:
                        is_batch = False
                        formatted_items = [{
                            "product_id": product_id,
                            "quantity": quantity or 1,
                            "modality": modality or "PICKUP",
                            "description": None,
                        }]
                    else:
                        return {
                            "success": False,
                            "error": "product_id (single mode) or items (batch mode) is required",
                        }

                    if preview_only:
                        pids = [item["product_id"] for item in formatted_items]
                        pantry_context = {}
                        try:
                            from ..analytics.pantry import get_pantry_item
                            for pid in pids:
                                pantry_item = get_pantry_item(pid)
                                if pantry_item:
                                    pantry_context[pid] = {
                                        "level_percent": pantry_item.get("level_percent", 0),
                                        "status": pantry_item.get("status"),
                                        "days_until_empty": pantry_item.get("days_until_empty"),
                                    }
                        except Exception:
                            pass

                        preview_items = []
                        skip_suggestions = []
                        for item in formatted_items:
                            pid = item["product_id"]
                            pantry = pantry_context.get(pid, {})
                            level = pantry.get("level_percent")
                            preview_item = {
                                **item,
                                "pantry_level": level,
                                "pantry_status": pantry.get("status"),
                            }
                            if level is not None and level >= 30:
                                preview_item["recommendation"] = "SKIP"
                                preview_item["reason"] = f"Pantry at {level}%"
                                skip_suggestions.append(preview_item)
                            else:
                                preview_item["recommendation"] = "ADD"
                            preview_items.append(preview_item)

                        return {
                            "success": True,
                            "preview_only": True,
                            "confirmation_required": True,
                            "items": preview_items,
                            "summary": {
                                "total_items": len(preview_items),
                                "items_to_add": len(
                                    [i for i in preview_items if i["recommendation"] == "ADD"]
                                ),
                                "items_to_skip": len(skip_suggestions),
                            },
                            "skip_suggestions": skip_suggestions,
                            "next_step": "Review and call again with preview_only=False to add",
                        }

                    filtering_enabled = is_filtering_enabled()
                    safety_warnings = []
                    blocked_items = []

                    if filtering_enabled and not (confirm_unsafe or False):
                        safe_ids = get_all_safe_product_ids()
                        blocked_ids_set = get_all_blocked_product_ids()
                        disabled_ingredients = get_disabled_ingredients()

                        for item in formatted_items:
                            pid = item["product_id"]
                            description = item.get("description") or ""

                            if pid in safe_ids:
                                continue
                            if pid in blocked_ids_set:
                                blocked_items.append({
                                    "product_id": pid,
                                    "description": description,
                                    "reason": "Product is on your blocked list",
                                })
                                continue
                            if description:
                                safety_result = check_product_safety(
                                    description=description,
                                    disabled_ingredients=disabled_ingredients,
                                )
                                if safety_result.has_concerns:
                                    safety_warnings.append({
                                        "product_id": pid,
                                        "description": description,
                                        "severity": safety_result.highest_severity.value,
                                        "flagged_ingredients": [
                                            {
                                                "ingredient": match.ingredient_name,
                                                "severity": match.severity.value,
                                                "reason": match.reason,
                                                "matched_text": match.matched_text,
                                            }
                                            for match in safety_result.matches
                                        ],
                                    })

                        if blocked_items or safety_warnings:
                            return {
                                "success": False,
                                "requires_confirmation": True,
                                "message": (
                                    "Some products have safety concerns. "
                                    "Set confirm_unsafe=True to add anyway."
                                ),
                                "blocked_items": blocked_items,
                                "safety_warnings": safety_warnings,
                                "total_flagged": len(blocked_items) + len(safety_warnings),
                                "items_requested": len(formatted_items),
                                "next_step": (
                                    "Review the flagged ingredients and either: "
                                    "(1) call again with confirm_unsafe=True to add anyway, "
                                    "(2) remove flagged items from your request, or "
                                    "(3) use safety(action='approve_product') to safe-list products you trust"
                                ),
                            }

                    if ctx:
                        await ctx.info(f"Adding {len(formatted_items)} item(s) to cart")

                    client = get_authenticated_client()
                    cart_items = [
                        {
                            "upc": item["product_id"],
                            "quantity": item["quantity"],
                            "modality": item["modality"],
                        }
                        for item in formatted_items
                    ]

                    if ctx:
                        await ctx.info(f"Calling Kroger API to add {len(cart_items)} item(s)")

                    client.cart.add_to_cart(cart_items)

                    if ctx:
                        await ctx.info("Successfully added item(s) to Kroger cart")

                    for item in formatted_items:
                        _add_item_to_local_cart(
                            item["product_id"], item["quantity"], item["modality"]
                        )

                    if ctx:
                        await ctx.info("Item(s) added to local cart tracking")

                    if is_batch:
                        return {
                            "success": True,
                            "message": f"Successfully added {len(formatted_items)} items to cart",
                            "items_added": len(formatted_items),
                            "items": formatted_items,
                            "timestamp": datetime.now().isoformat(),
                            "reminder": "Review your cart in the Kroger app before checkout",
                        }
                    else:
                        item = formatted_items[0]
                        return {
                            "success": True,
                            "message": (
                                f"Successfully added {item['quantity']}x "
                                f"{item['product_id']} to cart"
                            ),
                            "product_id": item["product_id"],
                            "quantity": item["quantity"],
                            "modality": item["modality"],
                            "timestamp": datetime.now().isoformat(),
                        }

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Failed to add item(s) to cart: {str(e)}")
                    error_message = str(e)
                    if "401" in error_message or "Unauthorized" in error_message:
                        return {
                            "success": False,
                            "error": "Authentication failed. Please run auth(action='force_reauth') and try again.",
                            "details": error_message,
                        }
                    elif "400" in error_message or "Bad Request" in error_message:
                        return {
                            "success": False,
                            "error": "Invalid request. Please check the product ID(s) and try again.",
                            "details": error_message,
                        }
                    else:
                        return {
                            "success": False,
                            "error": f"Failed to add item(s) to cart: {error_message}",
                        }

            case "remove":
                if not product_id:
                    return {"success": False, "error": "product_id is required"}
                try:
                    cart_data = _load_cart_data()
                    current_cart = cart_data.get("current_cart", [])
                    original_count = len(current_cart)

                    if modality:
                        cart_data["current_cart"] = [
                            item for item in current_cart
                            if not (
                                item.get("product_id") == product_id
                                and item.get("modality") == modality
                            )
                        ]
                    else:
                        cart_data["current_cart"] = [
                            item for item in current_cart
                            if item.get("product_id") != product_id
                        ]

                    items_removed = original_count - len(cart_data["current_cart"])
                    if items_removed > 0:
                        cart_data["last_updated"] = datetime.now().isoformat()
                        _save_cart_data(cart_data)

                    return {
                        "success": True,
                        "message": f"Removed {items_removed} items from local cart tracking",
                        "items_removed": items_removed,
                        "product_id": product_id,
                        "modality": modality,
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to remove from cart: {str(e)}"}

            case "clear":
                try:
                    cart_data = _load_cart_data()
                    items_count = len(cart_data.get("current_cart", []))
                    cart_data["current_cart"] = []
                    cart_data["last_updated"] = datetime.now().isoformat()
                    _save_cart_data(cart_data)
                    return {
                        "success": True,
                        "message": f"Cleared {items_count} items from local cart tracking",
                        "items_cleared": items_count,
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to clear cart: {str(e)}"}

            case "mark_placed":
                try:
                    cart_data = _load_cart_data()
                    current_cart = cart_data.get("current_cart", [])

                    if not current_cart:
                        return {
                            "success": False,
                            "error": "No items in current cart to mark as placed",
                        }

                    order_record = {
                        "items": current_cart.copy(),
                        "placed_at": datetime.now().isoformat(),
                        "item_count": len(current_cart),
                        "total_quantity": sum(
                            item.get("quantity", 0) for item in current_cart
                        ),
                        "notes": order_notes,
                    }

                    order_history = _load_order_history()
                    order_history.append(order_record)
                    _save_order_history(order_history)

                    analytics_order_id = None
                    try:
                        from ..analytics.purchase_tracker import record_order
                        from ..analytics.statistics import update_all_product_stats

                        analytics_order_id = record_order(current_cart, order_notes)
                        pids_in_order = [item.get("product_id") for item in current_cart]
                        update_all_product_stats(pids_in_order)
                    except Exception as e:
                        print(f"Warning: Could not record analytics: {e}")

                    cart_data["current_cart"] = []
                    cart_data["last_updated"] = datetime.now().isoformat()
                    _save_cart_data(cart_data)

                    return {
                        "success": True,
                        "message": f"Marked order with {order_record['item_count']} items as placed",
                        "order_id": len(order_history),
                        "analytics_order_id": analytics_order_id,
                        "items_placed": order_record["item_count"],
                        "total_quantity": order_record["total_quantity"],
                        "placed_at": order_record["placed_at"],
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to mark order as placed: {str(e)}",
                    }

            case "view_history":
                try:
                    hist_limit = max(1, min(50, limit or 10))
                    order_history = _load_order_history()
                    sorted_orders = sorted(
                        order_history,
                        key=lambda x: x.get("placed_at", ""),
                        reverse=True,
                    )
                    limited_orders = sorted_orders[:hist_limit]

                    total_orders = len(order_history)
                    total_items_all_time = sum(
                        order.get("item_count", 0) for order in order_history
                    )
                    total_quantity_all_time = sum(
                        order.get("total_quantity", 0) for order in order_history
                    )

                    return {
                        "success": True,
                        "orders": limited_orders,
                        "showing": len(limited_orders),
                        "summary": {
                            "total_orders": total_orders,
                            "total_items_all_time": total_items_all_time,
                            "total_quantity_all_time": total_quantity_all_time,
                        },
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to view order history: {str(e)}",
                    }

            case "get_context":
                try:
                    from ..analytics.pantry import get_pantry_status
                    from ..analytics.favorites import get_lists, get_list_items

                    threshold = pantry_threshold if pantry_threshold is not None else 30

                    result = {
                        "success": True,
                        "pantry_items": [],
                        "favorite_matches": [],
                        "skip_suggestions": [],
                        "low_inventory_alerts": [],
                        "summary": {},
                    }

                    all_pantry = get_pantry_status(apply_depletion=True)

                    if product_ids:
                        product_id_set = set(product_ids)
                        filtered_pantry = [
                            item for item in all_pantry
                            if item["product_id"] in product_id_set
                        ]
                    else:
                        filtered_pantry = all_pantry

                    result["pantry_items"] = filtered_pantry

                    for item in filtered_pantry:
                        level = item.get("level_percent", 0)
                        if level >= threshold:
                            result["skip_suggestions"].append({
                                "product_id": item["product_id"],
                                "description": item.get("description"),
                                "level_percent": level,
                                "reason": f"Pantry at {level}% (above {threshold}% threshold)",
                            })
                        elif level <= 20:
                            result["low_inventory_alerts"].append({
                                "product_id": item["product_id"],
                                "description": item.get("description"),
                                "level_percent": level,
                                "days_until_empty": item.get("days_until_empty"),
                                "urgency": "high" if level <= 10 else "medium",
                            })

                    all_lists = get_lists()
                    for fav_list in all_lists:
                        list_id = fav_list["id"]
                        list_items_result = get_list_items(
                            list_id, include_pantry_status=False
                        )
                        if list_items_result.get("success") and list_items_result.get("items"):
                            list_pids = {
                                item["product_id"]
                                for item in list_items_result["items"]
                            }
                            if product_ids:
                                matching_ids = list_pids.intersection(set(product_ids))
                            else:
                                matching_ids = list_pids
                            if matching_ids:
                                result["favorite_matches"].append({
                                    "list_id": list_id,
                                    "list_name": fav_list["name"],
                                    "matching_products": list(matching_ids),
                                    "match_count": len(matching_ids),
                                })

                    result["summary"] = {
                        "pantry_items_checked": len(filtered_pantry),
                        "items_to_skip": len(result["skip_suggestions"]),
                        "low_inventory_count": len(result["low_inventory_alerts"]),
                        "favorite_list_matches": len(result["favorite_matches"]),
                        "pantry_threshold_used": threshold,
                    }

                    if result["skip_suggestions"]:
                        result["guidance"] = (
                            f"You have {len(result['skip_suggestions'])} items that are "
                            f"well-stocked (>{threshold}%). Consider skipping these. "
                            "Ask the user to confirm before adding to cart."
                        )
                    elif result["low_inventory_alerts"]:
                        result["guidance"] = (
                            f"You have {len(result['low_inventory_alerts'])} items running low. "
                            "These should be prioritized for your next order."
                        )
                    else:
                        result["guidance"] = "No pantry data available for these products."

                    return result

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to get shopping context: {str(e)}",
                        "pantry_items": [],
                        "favorite_matches": [],
                        "skip_suggestions": [],
                        "low_inventory_alerts": [],
                    }

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
