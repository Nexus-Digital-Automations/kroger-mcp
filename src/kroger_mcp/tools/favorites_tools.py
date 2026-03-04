"""
Favorite lists MCP tools for the Kroger MCP server.
"""

from typing import Any, Dict, List, Literal, Optional

from fastmcp import Context
from pydantic import Field


def register_tools(mcp):
    """Register favorite list tools with the FastMCP server."""

    @mcp.tool()
    async def favorites(
        action: Literal[
            "create_list",
            "get_lists",
            "rename_list",
            "delete_list",
            "add_item",
            "remove_item",
            "get_items",
            "order",
            "suggest",
            "update_schedule",
        ] = Field(
            description=(
                "Action: 'create_list' - create a new favorite list, "
                "'get_lists' - get all favorite lists, "
                "'rename_list' - rename or update a list, "
                "'delete_list' - delete a list and its items, "
                "'add_item' - add product(s) to a list, "
                "'remove_item' - remove a product from a list, "
                "'get_items' - get all items in a list, "
                "'order' - add list items to cart, "
                "'suggest' - suggest products to add based on purchase history, "
                "'update_schedule' - update reorder schedule for a list"
            )
        ),
        name: Optional[str] = Field(
            default=None,
            description="List name e.g. 'Weekly Staples' (for create_list)",
        ),
        description: Optional[str] = Field(
            default=None,
            description="Optional list description (for create_list, rename_list)",
        ),
        list_type: Optional[str] = Field(
            default="custom",
            description="List type: 'custom', 'weekly', 'monthly', 'seasonal' (for create_list)",
        ),
        reorder_weeks: Optional[int] = Field(
            default=None,
            description="Reorder schedule in weeks 1-52, or null to disable (for create_list, update_schedule)",
        ),
        list_id: Optional[str] = Field(
            default="default",
            description="List ID to operate on (defaults to 'default')",
        ),
        new_name: Optional[str] = Field(
            default=None,
            description="New name for the list (for rename_list)",
        ),
        new_description: Optional[str] = Field(
            default=None,
            description="New description for the list (for rename_list)",
        ),
        product_id: Optional[str] = Field(
            default=None,
            description="Kroger product ID (for add_item single mode, remove_item)",
        ),
        brand: Optional[str] = Field(
            default=None,
            description="Product brand (for add_item single mode)",
        ),
        default_quantity: Optional[int] = Field(
            default=1,
            description="Default quantity when ordering (for add_item single mode)",
        ),
        preferred_modality: Optional[str] = Field(
            default="PICKUP",
            description="Preferred fulfillment: 'PICKUP' or 'DELIVERY' (for add_item)",
        ),
        notes: Optional[str] = Field(
            default=None,
            description="Optional notes about the item (for add_item single mode)",
        ),
        items: Optional[List[Dict[str, Any]]] = Field(
            default=None,
            description="For bulk add_item: list of {product_id, description, brand, default_quantity, preferred_modality, notes}",
        ),
        include_pantry_status: Optional[bool] = Field(
            default=True,
            description="Include current pantry levels (for get_items)",
        ),
        sort_by: Optional[str] = Field(
            default="description",
            description="Sort by: 'description', 'times_ordered', 'added_at' (for get_items)",
        ),
        skip_if_stocked: Optional[bool] = Field(
            default=True,
            description="Skip items with pantry level above threshold (for order)",
        ),
        pantry_threshold: Optional[int] = Field(
            default=30,
            description="Pantry level % above which to skip items (for order)",
        ),
        modality: Optional[str] = Field(
            default=None,
            description="Override all items' modality: 'PICKUP' or 'DELIVERY' (for order)",
        ),
        min_purchases: Optional[int] = Field(
            default=3,
            description="Minimum purchases to be suggested (for suggest)",
        ),
        min_frequency_score: Optional[float] = Field(
            default=0.5,
            description="Minimum frequency score 0-1 (for suggest)",
        ),
        limit: Optional[int] = Field(
            default=10,
            description="Maximum suggestions to return (for suggest)",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Favorite list management operations."""
        match action:
            case "create_list":
                if not name:
                    return {"success": False, "error": "name is required"}
                from ..analytics.favorites import create_list

                return create_list(
                    name=name,
                    description=description,
                    list_type=list_type or "custom",
                    reorder_weeks=reorder_weeks,
                )

            case "get_lists":
                from ..analytics.favorites import get_lists

                lists = get_lists()
                return {
                    "success": True,
                    "lists": lists,
                    "total_lists": len(lists),
                }

            case "rename_list":
                if not list_id:
                    return {"success": False, "error": "list_id is required"}
                from ..analytics.favorites import rename_list

                return rename_list(
                    list_id=list_id,
                    new_name=new_name,
                    new_description=new_description,
                )

            case "delete_list":
                if not list_id:
                    return {"success": False, "error": "list_id is required"}
                from ..analytics.favorites import delete_list

                return delete_list(list_id=list_id)

            case "add_item":
                from ..analytics.favorites import add_to_list, bulk_add_to_list

                if items is not None:
                    return bulk_add_to_list(list_id=list_id or "default", items=items)

                if not product_id or not description:
                    return {
                        "success": False,
                        "error": (
                            "For single item add, both product_id and description are required. "
                            "For bulk add, provide items list."
                        ),
                    }

                return add_to_list(
                    list_id=list_id or "default",
                    product_id=product_id,
                    description=description,
                    brand=brand,
                    default_quantity=default_quantity or 1,
                    preferred_modality=preferred_modality or "PICKUP",
                    notes=notes,
                )

            case "remove_item":
                if not product_id:
                    return {"success": False, "error": "product_id is required"}
                from ..analytics.favorites import remove_from_list

                return remove_from_list(
                    list_id=list_id or "default", product_id=product_id
                )

            case "get_items":
                from ..analytics.favorites import get_list_items

                return get_list_items(
                    list_id=list_id or "default",
                    include_pantry_status=include_pantry_status if include_pantry_status is not None else True,
                    sort_by=sort_by or "description",
                )

            case "order":
                from ..analytics.favorites import (
                    get_list,
                    get_list_items,
                    increment_times_ordered,
                    mark_list_ordered,
                )

                lid = list_id or "default"
                list_info = get_list(lid)
                if not list_info:
                    return {"success": False, "error": f"List '{lid}' not found"}

                result = get_list_items(lid, include_pantry_status=True)
                if not result.get("success"):
                    return result

                threshold = pantry_threshold if pantry_threshold is not None else 30
                do_skip = skip_if_stocked if skip_if_stocked is not None else True

                items_to_order = []
                items_skipped = []

                for item in result["items"]:
                    pantry = item.get("pantry_status", {})
                    level = pantry.get("level_percent")

                    should_skip = False
                    skip_reason = None

                    if do_skip and level is not None and level >= threshold:
                        should_skip = True
                        skip_reason = f"Pantry at {level}% (threshold: {threshold}%)"

                    if should_skip:
                        items_skipped.append(
                            {
                                "product_id": item["product_id"],
                                "description": item["description"],
                                "reason": skip_reason,
                                "pantry_level": level,
                            }
                        )
                    else:
                        items_to_order.append(
                            {
                                "upc": item["product_id"],
                                "quantity": item["default_quantity"],
                                "modality": modality or item["preferred_modality"],
                                "description": item["description"],
                                "product_id": item["product_id"],
                            }
                        )

                if not items_to_order:
                    return {
                        "success": True,
                        "message": "No items needed - all are well-stocked",
                        "items_ordered": [],
                        "items_skipped": items_skipped,
                        "order_count": 0,
                        "skip_count": len(items_skipped),
                        "reorder_status": list_info.get("reorder_status"),
                    }

                try:
                    from .shared import get_authenticated_client
                    from .cart_tools import _add_item_to_local_cart

                    client = get_authenticated_client()

                    cart_items = [
                        {
                            "upc": item["upc"],
                            "quantity": item["quantity"],
                            "modality": item["modality"],
                        }
                        for item in items_to_order
                    ]

                    client.cart.add_to_cart(cart_items)

                    for item in items_to_order:
                        _add_item_to_local_cart(
                            item["product_id"],
                            item["quantity"],
                            item["modality"],
                            {"description": item.get("description")},
                        )

                    ordered_ids = [i["product_id"] for i in items_to_order]
                    increment_times_ordered(lid, ordered_ids)

                    order_result = mark_list_ordered(lid)

                    response = {
                        "success": True,
                        "message": f"Added {len(items_to_order)} items, skipped {len(items_skipped)}",
                        "items_ordered": [
                            {
                                "product_id": i["product_id"],
                                "description": i["description"],
                                "quantity": i["quantity"],
                                "modality": i["modality"],
                            }
                            for i in items_to_order
                        ],
                        "items_skipped": items_skipped,
                        "order_count": len(items_to_order),
                        "skip_count": len(items_skipped),
                    }

                    if order_result.get("success"):
                        response["reorder_status"] = {
                            "was_overdue": order_result.get("was_overdue", False),
                            "ordered_at": order_result.get("ordered_at"),
                            "next_due": order_result.get("reorder_status", {}).get(
                                "next_due_date"
                            ),
                            "schedule_weeks": order_result.get("reorder_status", {}).get(
                                "reorder_weeks"
                            ),
                        }

                        if order_result.get("was_overdue"):
                            response["message"] += " (This list was OVERDUE for reorder)"

                    return response

                except Exception as e:
                    error_msg = str(e)
                    if "401" in error_msg or "Unauthorized" in error_msg:
                        return {
                            "success": False,
                            "error": "Authentication failed. Run auth(action='force_reauth').",
                            "details": error_msg,
                        }
                    return {
                        "success": False,
                        "error": f"Failed to add items to cart: {error_msg}",
                        "items_to_order": [
                            {"product_id": i["product_id"], "description": i["description"]}
                            for i in items_to_order
                        ],
                        "items_skipped": items_skipped,
                    }

            case "suggest":
                from ..analytics.favorites import suggest_for_list

                return suggest_for_list(
                    list_id=list_id,
                    min_purchases=min_purchases or 3,
                    min_frequency_score=min_frequency_score if min_frequency_score is not None else 0.5,
                    limit=limit or 10,
                )

            case "update_schedule":
                if not list_id:
                    return {"success": False, "error": "list_id is required"}
                from ..analytics.favorites import update_list_schedule

                return update_list_schedule(
                    list_id=list_id,
                    reorder_weeks=reorder_weeks,
                )

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
