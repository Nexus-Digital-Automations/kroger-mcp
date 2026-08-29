"""
Favorite lists MCP tools for the Kroger MCP server.
"""

import asyncio
import logging
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from ._cart_safety import check_cart_items_safety

logger = logging.getLogger(__name__)


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
            "set_stock_level",
            "update_quantity",
            "get_low_stock",
            "check_snacks",
        ] = Field(
            description=(
                "order — ADD ENTIRE LIST TO CART in one call (skips well-stocked items). "
                "Use this whenever user asks to order/add a favorites list. "
                "check_snacks — pre-cart replenishment checklist for the Snacks list: "
                "returns each snack with a pre_ticked guess (pantry-low, stale, or never ordered) "
                "for the user to confirm before sending the list to cart. "
                "set_stock_level — set min_stock_percent and/or min_stock_quantity thresholds per item. "
                "update_quantity — update current_stock_quantity for an item. "
                "get_low_stock — list items below their minimum thresholds. "
                "Other actions: get_lists|get_items|add_item|remove_item|create_list|rename_list|delete_list|suggest|update_schedule"
            )
        ),
        name: str | None = Field(
            default=None,
            description="List name",
        ),
        description: str | None = Field(
            default=None,
            description="List description",
        ),
        list_type: str | None = Field(
            default="custom",
            description="custom|weekly|monthly|seasonal",
        ),
        reorder_weeks: int | None = Field(
            default=None,
            description="Reorder schedule in weeks 1-52",
        ),
        list_id: str | None = Field(
            default="default",
            description="List ID (defaults to 'default')",
        ),
        new_name: str | None = Field(
            default=None,
            description="New list name",
        ),
        new_description: str | None = Field(
            default=None,
            description="New list description",
        ),
        product_id: str | None = Field(
            default=None,
            description="Kroger product ID",
        ),
        product_ids: list[str] | None = Field(
            default=None,
            description="Batch remove: list of Kroger product IDs",
        ),
        brand: str | None = Field(
            default=None,
            description="Product brand",
        ),
        default_quantity: int | None = Field(
            default=1,
            description="Default order quantity",
        ),
        preferred_modality: str | None = Field(
            default="PICKUP",
            description="PICKUP or DELIVERY",
        ),
        notes: str | None = Field(
            default=None,
            description="Item notes",
        ),
        items: list[dict[str, Any]] | None = Field(
            default=None,
            description="Bulk add: [{product_id, description, brand, default_quantity, preferred_modality, notes, min_stock_percent, min_stock_quantity, current_stock_quantity, manual, override_reason}]",
        ),
        manual: bool | None = Field(
            default=False,
            description=(
                "add_item — True for an item Kroger doesn't sell (farmers market, "
                "home-grown, specialty butcher). No product_id needed; it is stored "
                "as a MANUAL PURCHASE item and never sent to the Kroger cart."
            ),
        ),
        override_reason: str | None = Field(
            default=None,
            description="add_item — optional note on why a manual item isn't a Kroger product",
        ),
        min_stock_percent: int | None = Field(
            default=None,
            description="Per-item reorder trigger: include in order if pantry < this % (None = use global threshold)",
        ),
        min_stock_quantity: int | None = Field(
            default=None,
            description="Target on-hand unit count — reorder if current_stock_quantity < this",
        ),
        current_stock_quantity: int | None = Field(
            default=None,
            description="Actual on-hand unit count (user-managed)",
        ),
        typical_gap_days: int | None = Field(
            default=None,
            description="Snacks: typical days between buys; staleness threshold for the check-up (default 21)",
        ),
        include_pantry_status: bool | None = Field(
            default=True,
            description="Include pantry levels",
        ),
        sort_by: str | None = Field(
            default="description",
            description="description|times_ordered|added_at",
        ),
        skip_if_stocked: bool | None = Field(
            default=True,
            description="Skip well-stocked items",
        ),
        pantry_threshold: int | None = Field(
            default=30,
            description="Skip if pantry level above this %",
        ),
        modality: str | None = Field(
            default=None,
            description="PICKUP or DELIVERY override",
        ),
        min_purchases: int | None = Field(
            default=3,
            description="Min purchases to suggest",
        ),
        min_frequency_score: float | None = Field(
            default=0.5,
            description="Min frequency score 0-1",
        ),
        limit: int | None = Field(
            default=10,
            description="Max suggestions to return",
        ),
        confirm: bool | None = Field(
            default=False,
            description="order — False=preview, True=execute (add to cart)",
        ),
        confirm_unsafe: bool | None = Field(
            default=False,
            description="order — override safety warnings",
        ),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Favorite list management operations.

        IMPORTANT — To add a favorites list to cart:
          favorites(action='order', list_id='weekly-essentials', confirm=True)
        This adds ALL items in one call, skipping well-stocked pantry items.
        Call with confirm=False (default) first to preview what will be ordered.
        Do NOT use get_items + loop cart(action='add') — use order instead.

        Other actions: get_lists, get_items, add_item, remove_item, create_list,
        rename_list, delete_list, suggest, update_schedule
        """
        return await asyncio.to_thread(
            _favorites_impl,
            action,
            name,
            description,
            list_type,
            reorder_weeks,
            list_id,
            new_name,
            new_description,
            product_id,
            product_ids,
            brand,
            default_quantity,
            preferred_modality,
            notes,
            items,
            manual,
            override_reason,
            min_stock_percent,
            min_stock_quantity,
            current_stock_quantity,
            typical_gap_days,
            include_pantry_status,
            sort_by,
            skip_if_stocked,
            pantry_threshold,
            modality,
            min_purchases,
            min_frequency_score,
            limit,
            confirm,
            confirm_unsafe,
            ctx,
        )

    def _favorites_impl(
        action,
        name,
        description,
        list_type,
        reorder_weeks,
        list_id,
        new_name,
        new_description,
        product_id,
        product_ids,
        brand,
        default_quantity,
        preferred_modality,
        notes,
        items,
        manual,
        override_reason,
        min_stock_percent,
        min_stock_quantity,
        current_stock_quantity,
        typical_gap_days,
        include_pantry_status,
        sort_by,
        skip_if_stocked,
        pantry_threshold,
        modality,
        min_purchases,
        min_frequency_score,
        limit,
        confirm,
        confirm_unsafe,
        ctx,
    ):
        from kroger_mcp.auth.dependencies import mcp_user_id

        user_id = mcp_user_id()

        # "default" is a tool-parameter sentinel, not a real list id (real
        # default lists are `default-<uuid>` per user) -- resolve it up front
        # so no downstream query ever queries/writes against the literal
        # string (which would either no-op or, in an aggregate query, be
        # tempted to skip user scoping entirely).
        if list_id in (None, "default") and action not in ("create_list", "get_lists"):
            from ..analytics.favorites import resolve_default_list_id

            list_id = resolve_default_list_id(user_id=user_id)

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
                    user_id=user_id,
                )

            case "get_lists":
                from ..analytics.favorites import get_lists

                lists = get_lists(user_id=user_id)
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
                    user_id=user_id,
                )

            case "delete_list":
                if not list_id:
                    return {"success": False, "error": "list_id is required"}
                from ..analytics.favorites import delete_list

                return delete_list(list_id=list_id, user_id=user_id)

            case "add_item":
                from ..analytics.favorites import add_to_list, bulk_add_to_list

                if items is not None:
                    return bulk_add_to_list(
                        list_id=list_id or "default", items=items, user_id=user_id
                    )

                if not description or (not product_id and not manual):
                    return {
                        "success": False,
                        "error": (
                            "For single item add, both product_id and description are required "
                            "— unless the item isn't sold at Kroger, in which case pass "
                            "manual=True with a description (product_id not needed). "
                            "For bulk add, provide items list."
                        ),
                    }

                return add_to_list(
                    list_id=list_id or "default",
                    product_id=product_id,
                    description=description,
                    manual=bool(manual),
                    override_reason=override_reason,
                    brand=brand,
                    default_quantity=default_quantity or 1,
                    preferred_modality=preferred_modality or "PICKUP",
                    notes=notes,
                    min_stock_percent=min_stock_percent,
                    min_stock_quantity=min_stock_quantity,
                    current_stock_quantity=current_stock_quantity,
                    typical_gap_days=typical_gap_days,
                    user_id=user_id,
                )

            case "remove_item":
                from ..analytics.favorites import remove_from_list

                ids = product_ids if product_ids else ([product_id] if product_id else None)
                if not ids:
                    return {"success": False, "error": "product_id or product_ids is required"}

                if len(ids) == 1:
                    return remove_from_list(
                        list_id=list_id or "default", product_id=ids[0], user_id=user_id
                    )

                results = {
                    pid: remove_from_list(
                        list_id=list_id or "default", product_id=pid, user_id=user_id
                    )
                    for pid in ids
                }
                removed = sum(1 for r in results.values() if r.get("success"))
                return {
                    "success": True,
                    "removed": removed,
                    "total": len(ids),
                    "results": results,
                }

            case "get_items":
                from ..analytics.favorites import get_list_items

                return get_list_items(
                    list_id=list_id or "default",
                    include_pantry_status=(
                        include_pantry_status if include_pantry_status is not None else True
                    ),
                    sort_by=sort_by or "description",
                    user_id=user_id,
                )

            case "order":
                from ..analytics.favorites import (
                    get_list,
                    get_list_items,
                    increment_times_ordered,
                    mark_list_ordered,
                )

                lid = list_id or "default"
                list_info = get_list(lid, user_id=user_id)
                if not list_info:
                    return {"success": False, "error": f"List '{lid}' not found"}

                result = get_list_items(lid, include_pantry_status=True, user_id=user_id)
                if not result.get("success"):
                    return result

                threshold = pantry_threshold if pantry_threshold is not None else 30
                do_skip = skip_if_stocked if skip_if_stocked is not None else True

                items_to_order = []
                items_skipped = []
                # Items with no Kroger product behind them. Reported separately
                # so the user still sees what they have to source themselves —
                # mirrors recipes(action='preview_order')'s manual_purchase list.
                manual_purchase = [
                    {
                        "product_id": item["product_id"],
                        "description": item["description"],
                        "quantity": item["default_quantity"],
                        "override_reason": item.get("override_reason"),
                        "action": "MANUAL",
                    }
                    for item in result["items"]
                    if item.get("is_manual")
                ]

                for item in result["items"]:
                    if item.get("is_manual"):
                        continue
                    pantry = item.get("pantry_status", {})
                    level = pantry.get("level_percent")
                    min_pct = item.get("min_stock_percent")
                    min_qty = item.get("min_stock_quantity")
                    cur_qty = item.get("current_stock_quantity")

                    needs_restock = False
                    restock_reasons = []

                    if min_pct is not None:
                        if level is None or level < min_pct:
                            needs_restock = True
                            restock_reasons.append(
                                f"Pantry {level if level is not None else 0}% < minimum {min_pct}%"
                            )

                    if min_qty is not None:
                        if cur_qty is None or cur_qty < min_qty:
                            needs_restock = True
                            restock_reasons.append(
                                f"Have {cur_qty if cur_qty is not None else 0} units, minimum is {min_qty}"
                            )

                    if min_pct is None and min_qty is None:
                        # No per-item minimums — fall back to global pantry threshold
                        if do_skip and level is not None and level >= threshold:
                            pass  # will be skipped below
                        else:
                            needs_restock = True
                        if do_skip and level is not None and level >= threshold:
                            restock_reasons = [f"Pantry at {level}% (threshold: {threshold}%)"]

                    should_skip = not needs_restock

                    if should_skip:
                        items_skipped.append(
                            {
                                "product_id": item["product_id"],
                                "description": item["description"],
                                "reason": restock_reasons[0] if restock_reasons else "Well stocked",
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
                        "message": (
                            "No items needed - all are well-stocked"
                            + (
                                f". {len(manual_purchase)} item(s) require manual purchase."
                                if manual_purchase
                                else ""
                            )
                        ),
                        "items_ordered": [],
                        "items_skipped": items_skipped,
                        "manual_purchase": manual_purchase,
                        "order_count": 0,
                        "skip_count": len(items_skipped),
                        "manual_count": len(manual_purchase),
                        "reorder_status": list_info.get("reorder_status"),
                    }

                if not confirm:
                    return {
                        "success": True,
                        "confirmation_required": True,
                        "preview": {
                            "items_to_order": [
                                {
                                    "product_id": i["product_id"],
                                    "description": i["description"],
                                    "quantity": i["quantity"],
                                    "modality": i["modality"],
                                }
                                for i in items_to_order
                            ],
                            "items_skipped": items_skipped,
                            "manual_purchase": manual_purchase,
                            "order_count": len(items_to_order),
                            "skip_count": len(items_skipped),
                            "manual_count": len(manual_purchase),
                        },
                        "next_step": (
                            "Review the items above. Call again with confirm=True to add to cart."
                            + (
                                " Items under MANUAL PURCHASE are not sold at Kroger — "
                                "you'll need to source those yourself."
                                if manual_purchase
                                else ""
                            )
                        ),
                    }

                safety_response = check_cart_items_safety(
                    [
                        {"product_id": item["product_id"], "description": item.get("description", "")}
                        for item in items_to_order
                    ],
                    user_id=user_id,
                    confirm_unsafe=bool(confirm_unsafe),
                )
                if safety_response is not None:
                    return safety_response

                try:
                    from .cart_tools import _add_item_to_local_cart
                    from .shared import get_authenticated_client

                    client = get_authenticated_client(user_id=user_id)

                    cart_items = [
                        {
                            "upc": item["upc"],
                            "quantity": item["quantity"],
                            "modality": item["modality"],
                        }
                        for item in items_to_order
                    ]

                    client.cart.add_to_cart(cart_items)

                    local_tracking_warning = None
                    order_result: dict[str, Any] = {}
                    try:
                        for item in items_to_order:
                            _add_item_to_local_cart(
                                item["product_id"],
                                item["quantity"],
                                item["modality"],
                                {"description": item.get("description")},
                                user_id=user_id,
                            )

                        ordered_ids = [i["product_id"] for i in items_to_order]
                        increment_times_ordered(lid, ordered_ids, user_id=user_id)

                        order_result = mark_list_ordered(lid, user_id=user_id)
                    except Exception as tracking_err:
                        # The real Kroger order already succeeded above — a
                        # local-tracking failure must never make this report
                        # success=False, or a caller retrying on "failure"
                        # would place a duplicate real order.
                        logger.error(
                            f"Local order tracking failed for list {lid} after a "
                            f"successful Kroger order: {tracking_err}"
                        )
                        local_tracking_warning = str(tracking_err)

                    response = {
                        "success": True,
                        "message": (
                            f"Added {len(items_to_order)} items, "
                            f"skipped {len(items_skipped)}"
                            + (
                                f", {len(manual_purchase)} require manual purchase"
                                if manual_purchase
                                else ""
                            )
                        ),
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
                        "manual_purchase": manual_purchase,
                        "order_count": len(items_to_order),
                        "skip_count": len(items_skipped),
                        "manual_count": len(manual_purchase),
                    }

                    if order_result.get("success"):
                        response["reorder_status"] = {
                            "was_overdue": order_result.get("was_overdue", False),
                            "ordered_at": order_result.get("ordered_at"),
                            "next_due": order_result.get("reorder_status", {}).get("next_due_date"),
                            "schedule_weeks": order_result.get("reorder_status", {}).get(
                                "reorder_weeks"
                            ),
                        }

                        if order_result.get("was_overdue"):
                            response["message"] += " (This list was OVERDUE for reorder)"

                    if local_tracking_warning:
                        response["local_tracking_warning"] = (
                            "Kroger order succeeded, but local tracking failed: "
                            f"{local_tracking_warning}"
                        )

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
                        "manual_purchase": manual_purchase,
                    }

            case "suggest":
                from ..analytics.favorites import suggest_for_list

                return suggest_for_list(
                    list_id=list_id,
                    min_purchases=min_purchases or 3,
                    min_frequency_score=(
                        min_frequency_score if min_frequency_score is not None else 0.5
                    ),
                    limit=limit or 10,
                    user_id=user_id,
                )

            case "update_schedule":
                if not list_id:
                    return {"success": False, "error": "list_id is required"}
                from ..analytics.favorites import update_list_schedule

                return update_list_schedule(
                    list_id=list_id,
                    reorder_weeks=reorder_weeks,
                    user_id=user_id,
                )

            case "set_stock_level":
                if not list_id or not product_id:
                    return {"success": False, "error": "list_id and product_id are required"}
                from ..analytics.favorites import update_list_item

                return update_list_item(
                    list_id=list_id,
                    product_id=product_id,
                    min_stock_percent=min_stock_percent,
                    min_stock_quantity=min_stock_quantity,
                    current_stock_quantity=current_stock_quantity,
                    typical_gap_days=typical_gap_days,
                    user_id=user_id,
                )

            case "update_quantity":
                if not list_id or not product_id:
                    return {"success": False, "error": "list_id and product_id are required"}
                if current_stock_quantity is None:
                    return {"success": False, "error": "current_stock_quantity is required"}
                from ..analytics.favorites import update_list_item

                return update_list_item(
                    list_id=list_id,
                    product_id=product_id,
                    current_stock_quantity=current_stock_quantity,
                    user_id=user_id,
                )

            case "get_low_stock":
                if not list_id:
                    return {"success": False, "error": "list_id is required"}
                from ..analytics.favorites import get_low_stock_items

                return get_low_stock_items(list_id=list_id, user_id=user_id)

            case "check_snacks":
                from ..analytics.favorites import check_snacks

                return check_snacks(user_id=user_id)

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
