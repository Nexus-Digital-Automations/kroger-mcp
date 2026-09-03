"""
Meal planner tools for creating and managing meal plans.
"""

import asyncio
from datetime import datetime
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from ..analytics import meal_planning
from ..auth.dependencies import mcp_user_id
from .shared import get_authenticated_client


def register_tools(mcp):
    """Register meal planner tools with the FastMCP server."""

    @mcp.tool()
    async def meal_plan(
        action: Literal[
            "create",
            "list",
            "get",
            "update",
            "delete",
            "copy",
            "assign_meal",
            "remove_meal",
            "swap",
            "mark_cooked",
            "preview_shopping",
            "add_to_cart",
            "get_week_view",
            "get_summary",
            "generate_draft",
            "approve_draft",
            "skip_meal",
            "undo_cooked",
        ] = Field(
            description=(
                "preview_shopping — see ingredients needed with pantry status. "
                "add_to_cart — add plan ingredients (confirm=True to execute). "
                "assign_meal — add recipe to date/slot. "
                "mark_cooked — mark/unmark a meal as cooked (deducts pantry). "
                "generate_draft — auto-draft next week's dinners from saved recipes. "
                "approve_draft — promote a draft plan (needs plan_id). "
                "skip_meal — skip a pending past meal, no pantry deduction "
                "(plan_id+meal_date+meal_slot). "
                "undo_cooked — revert a cooked meal and restore pantry "
                "(plan_id+meal_date+meal_slot). "
                "Other: create|list|get|update|delete|copy|remove_meal|swap|get_week_view|get_summary"
            )
        ),
        plan_id: str | None = Field(
            default=None,
            description="Plan identifier",
        ),
        name: str | None = Field(
            default=None,
            description="Plan name",
        ),
        start_date: str | None = Field(
            default=None,
            description="Start date YYYY-MM-DD",
        ),
        end_date: str | None = Field(
            default=None,
            description="End date YYYY-MM-DD",
        ),
        plan_type: str | None = Field(
            default="weekly",
            description="weekly|monthly|custom",
        ),
        description: str | None = Field(
            default=None,
            description="Plan description",
        ),
        is_template: bool | None = Field(
            default=False,
            description="Save as reusable template",
        ),
        include_past: bool | None = Field(
            default=False,
            description="Include past plans",
        ),
        include_templates: bool | None = Field(
            default=False,
            description="Include template plans",
        ),
        limit: int | None = Field(
            default=20,
            description="Max plans to return",
        ),
        include_recipe_details: bool | None = Field(
            default=True,
            description="Include recipe names and servings",
        ),
        source_plan_id: str | None = Field(
            default=None,
            description="Plan to copy from",
        ),
        new_name: str | None = Field(
            default=None,
            description="Name for copied plan",
        ),
        new_start_date: str | None = Field(
            default=None,
            description="Start date for copied plan YYYY-MM-DD",
        ),
        recipe_id: str | None = Field(
            default=None,
            description="Recipe to assign",
        ),
        meal_date: str | None = Field(
            default=None,
            description="Date YYYY-MM-DD",
        ),
        meal_slot: str | None = Field(
            default=None,
            description="breakfast|lunch|dinner|snack",
        ),
        servings_override: int | None = Field(
            default=None,
            description="Override recipe servings",
        ),
        notes: str | None = Field(
            default=None,
            description="Optional notes",
        ),
        assignments: list[dict[str, Any]] | None = Field(
            default=None,
            description="Batch: [{recipe_id, meal_date, meal_slot, servings_override, notes}] max 100",
        ),
        date1: str | None = Field(
            default=None,
            description="First swap date YYYY-MM-DD",
        ),
        slot1: str | None = Field(
            default=None,
            description="First swap slot",
        ),
        date2: str | None = Field(
            default=None,
            description="Second swap date YYYY-MM-DD",
        ),
        slot2: str | None = Field(
            default=None,
            description="Second swap slot",
        ),
        days_ahead: int | None = Field(
            default=None,
            description="Days ahead to include",
        ),
        pantry_threshold: int | None = Field(
            default=30,
            description="Skip if pantry above this %",
        ),
        combine_duplicates: bool | None = Field(
            default=True,
            description="Merge same ingredients across recipes",
        ),
        skip_items: list[str] | None = Field(
            default=None,
            description="Ingredient names to skip",
        ),
        modality: str | None = Field(
            default="PICKUP",
            description="PICKUP or DELIVERY",
        ),
        confirm: bool | None = Field(
            default=False,
            description="True to confirm add after preview",
        ),
        week_start_date: str | None = Field(
            default=None,
            description="Week Monday YYYY-MM-DD",
        ),
        cooked: bool | None = Field(
            default=True,
            description="mark_cooked: True to mark as cooked, False to unmark",
        ),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Meal plan management with integrated shopping.

        assign_meal — add recipes to dates/slots (breakfast|lunch|dinner|snack).
        Recipes auto-scale to household servings (set via info tool).
        preview_shopping — consolidated ingredient list with pantry status.
        add_to_cart — add plan ingredients to cart (confirm=True to execute).

        CRUD: create|list|get|update|delete|copy.
        Views: get_week_view|get_summary.
        Meals: assign_meal|remove_meal|swap.
        """
        return await asyncio.to_thread(
            _meal_plan_impl,
            action,
            plan_id,
            name,
            start_date,
            end_date,
            plan_type,
            description,
            is_template,
            include_past,
            include_templates,
            limit,
            include_recipe_details,
            source_plan_id,
            new_name,
            new_start_date,
            recipe_id,
            meal_date,
            meal_slot,
            servings_override,
            notes,
            assignments,
            date1,
            slot1,
            date2,
            slot2,
            days_ahead,
            pantry_threshold,
            combine_duplicates,
            skip_items,
            modality,
            confirm,
            week_start_date,
            cooked,
            ctx,
        )

    def _meal_plan_impl(
        action,
        plan_id,
        name,
        start_date,
        end_date,
        plan_type,
        description,
        is_template,
        include_past,
        include_templates,
        limit,
        include_recipe_details,
        source_plan_id,
        new_name,
        new_start_date,
        recipe_id,
        meal_date,
        meal_slot,
        servings_override,
        notes,
        assignments,
        date1,
        slot1,
        date2,
        slot2,
        days_ahead,
        pantry_threshold,
        combine_duplicates,
        skip_items,
        modality,
        confirm,
        week_start_date,
        cooked,
        ctx,
    ):
        user_id = mcp_user_id()

        # Lazy passive-deduction catch-up: viewing plans is a natural moment to
        # settle past meals. Fire-and-discard — a reconcile hiccup must never
        # break a read action.
        if action in ("list", "get", "get_week_view"):
            try:
                meal_planning.reconcile_past_meals(user_id=user_id)
            except Exception:
                pass

        match action:
            case "create":
                if not name:
                    return {"success": False, "error": "name is required"}
                if not start_date:
                    return {"success": False, "error": "start_date is required"}

                result = meal_planning.create_meal_plan(
                    name=name,
                    start_date=start_date,
                    end_date=end_date,
                    plan_type=plan_type or "weekly",
                    description=description,
                    is_template=is_template or False,
                    user_id=user_id,
                )

                if ctx and result.get("success"):
                    ctx.info(f"Created meal plan '{name}'")

                return result

            case "list":
                return meal_planning.get_meal_plans(
                    include_past=include_past or False,
                    include_templates=include_templates or False,
                    limit=limit or 20,
                    user_id=user_id,
                )

            case "get":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}

                return meal_planning.get_meal_plan(
                    plan_id=plan_id,
                    include_recipe_details=(
                        include_recipe_details if include_recipe_details is not None else True
                    ),
                    user_id=user_id,
                )

            case "update":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}

                return meal_planning.update_meal_plan(
                    plan_id=plan_id,
                    name=name,
                    description=description,
                    start_date=start_date,
                    end_date=end_date,
                    user_id=user_id,
                )

            case "delete":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}

                result = meal_planning.delete_meal_plan(plan_id, user_id=user_id)

                if ctx and result.get("success"):
                    ctx.info("Deleted meal plan")

                return result

            case "copy":
                if not source_plan_id:
                    return {"success": False, "error": "source_plan_id is required"}
                if not new_name:
                    return {"success": False, "error": "new_name is required"}
                if not new_start_date:
                    return {"success": False, "error": "new_start_date is required"}

                result = meal_planning.copy_meal_plan(
                    source_plan_id=source_plan_id,
                    new_name=new_name,
                    new_start_date=new_start_date,
                    user_id=user_id,
                )

                if ctx and result.get("success"):
                    ctx.info(f"Copied plan with {result.get('meals_copied', 0)} meals")

                return result

            case "assign_meal":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}

                if assignments:
                    if len(assignments) > 100:
                        return {
                            "success": False,
                            "error": "Maximum 100 assignments per batch request",
                        }

                    result = meal_planning.bulk_assign_meals(
                        plan_id=plan_id, assignments=assignments, user_id=user_id
                    )

                    if ctx and result.get("success"):
                        ctx.info(f"Assigned {result.get('assigned', 0)} meals")

                    return result

                if not all([recipe_id, meal_date, meal_slot]):
                    return {
                        "success": False,
                        "error": (
                            "For single mode, provide recipe_id, meal_date, and meal_slot. "
                            "For batch mode, provide assignments list."
                        ),
                    }

                result = meal_planning.assign_meal(
                    plan_id=plan_id,
                    recipe_id=recipe_id,
                    meal_date=meal_date,
                    meal_slot=meal_slot,
                    servings_override=servings_override,
                    notes=notes,
                    user_id=user_id,
                )

                if ctx and result.get("success"):
                    ctx.info(f"Assigned '{result.get('recipe_name')}' to {meal_slot}")

                return result

            case "remove_meal":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}
                if not meal_date:
                    return {"success": False, "error": "meal_date is required"}
                if not meal_slot:
                    return {"success": False, "error": "meal_slot is required"}

                return meal_planning.remove_meal(
                    plan_id=plan_id,
                    meal_date=meal_date,
                    meal_slot=meal_slot,
                    user_id=user_id,
                )

            case "swap":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}
                if not all([date1, slot1, date2, slot2]):
                    return {
                        "success": False,
                        "error": "date1, slot1, date2, slot2 are all required",
                    }

                return meal_planning.swap_meals(
                    plan_id=plan_id,
                    date1=date1,
                    slot1=slot1,
                    date2=date2,
                    slot2=slot2,
                    user_id=user_id,
                )

            case "mark_cooked":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}
                if not meal_date:
                    return {"success": False, "error": "meal_date is required"}
                if not meal_slot:
                    return {"success": False, "error": "meal_slot is required"}

                mark = cooked if cooked is not None else True

                if not mark:
                    # Unmark: clear cooked_at via direct DB update (owner-scoped)
                    from ..analytics.database import ensure_initialized, get_db_connection

                    ensure_initialized()
                    conn = get_db_connection()
                    try:
                        conn.execute(
                            "UPDATE meal_entries SET cooked_at = NULL "
                            "WHERE plan_id = ? AND meal_date = ? AND meal_slot = ? "
                            "AND user_id = ?",
                            (plan_id, meal_date, meal_slot, user_id),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    return {
                        "success": True,
                        "plan_id": plan_id,
                        "meal_date": meal_date,
                        "meal_slot": meal_slot,
                        "cooked": False,
                        "message": f"Unmarked {meal_slot} on {meal_date} as cooked",
                    }

                return meal_planning.mark_meal_cooked(
                    plan_id=plan_id,
                    meal_date=meal_date,
                    meal_slot=meal_slot,
                    user_id=user_id,
                )

            case "preview_shopping":
                return meal_planning.generate_meal_plan_shopping_list(
                    plan_id=plan_id,
                    start_date=start_date,
                    end_date=end_date,
                    days_ahead=days_ahead,
                    pantry_threshold=pantry_threshold if pantry_threshold is not None else 30,
                    combine_duplicates=(
                        combine_duplicates if combine_duplicates is not None else True
                    ),
                    skip_items=skip_items,
                    user_id=user_id,
                )

            case "add_to_cart":
                shopping = meal_planning.generate_meal_plan_shopping_list(
                    plan_id=plan_id,
                    start_date=start_date,
                    end_date=end_date,
                    days_ahead=days_ahead,
                    pantry_threshold=pantry_threshold if pantry_threshold is not None else 30,
                    combine_duplicates=True,
                    skip_items=skip_items,
                    user_id=user_id,
                )

                if not shopping.get("success"):
                    return shopping

                items_to_add = shopping.get("items_to_add", [])
                items_to_skip = shopping.get("items_to_skip", [])
                items_unknown = shopping.get("items_unknown", [])
                mod = modality or "PICKUP"

                if not (confirm or False):
                    return {
                        "success": True,
                        "confirmation_required": True,
                        "preview": {
                            "date_range": shopping.get("date_range"),
                            "meals_included": shopping.get("meals_included"),
                            "recipes_included": shopping.get("recipes_included"),
                            "modality": mod,
                            "ingredients": shopping.get("ingredients", []),
                            "summary": shopping.get("summary", {}),
                        },
                        "items_to_add": items_to_add,
                        "items_to_skip": items_to_skip,
                        "items_unknown": items_unknown,
                        "next_step": (
                            "Review the ingredients above. "
                            "Call this tool again with confirm=True to add items to cart. "
                            "Use skip_items to exclude any additional items. "
                            "Items marked UNKNOWN need product linking via recipes(action='link_ingredient')."
                        ),
                    }

                if not items_to_add:
                    return {
                        "success": True,
                        "message": (
                            "No items to add - all ingredients are well-stocked, "
                            "skipped, or need product linking"
                        ),
                        "items_ordered": [],
                        "items_skipped": [i["name"] for i in items_to_skip],
                        "items_unknown": [i["name"] for i in items_unknown],
                    }

                if ctx:
                    ctx.info(f"Adding {len(items_to_add)} items to cart...")

                try:
                    client = get_authenticated_client(user_id)

                    api_items = [
                        {
                            "upc": item["product_id"],
                            "quantity": max(1, int(round(item.get("quantity", 1)))),
                            "modality": mod,
                        }
                        for item in items_to_add
                        if item.get("product_id")
                    ]

                    if not api_items:
                        return {
                            "success": False,
                            "error": "No items with product IDs to add",
                            "items_unknown": [i["name"] for i in items_unknown],
                        }

                    # Unlike the other cart-write paths this one has no
                    # check_cart_items_safety() call to hang the manual-item
                    # check off, so it is inlined. A manual favorite's id can
                    # reach here by being linked to a recipe ingredient that the
                    # plan then pulls in. Uses the shared predicate so an
                    # unlinked (product_id-less) item is caught too, not just a
                    # `manual:` id — the api_items comprehension above filters
                    # those out, but this must not depend on that staying true.
                    from ..analytics.manual_sources import is_manual_item

                    manual_upcs = [
                        i["upc"] for i in api_items if is_manual_item({"product_id": i["upc"]})
                    ]
                    if manual_upcs:
                        return {
                            "success": False,
                            "error": (
                                "These are manual items not sold at Kroger and cannot be "
                                f"added to the cart: {', '.join(manual_upcs)}. "
                                "You'll need to source them yourself."
                            ),
                            "manual_items": manual_upcs,
                        }

                    client.cart.add_to_cart(api_items)

                    from .cart_tools import _add_item_to_local_cart

                    # The real Kroger order above already succeeded — a
                    # local-tracking failure below must never flip this to
                    # success=False, or a retry would duplicate the order.
                    local_tracking_warning = None
                    try:
                        for item in items_to_add:
                            if item.get("product_id"):
                                _add_item_to_local_cart(
                                    item["product_id"],
                                    max(1, int(round(item.get("quantity", 1)))),
                                    mod,
                                    user_id=user_id,
                                )
                        if plan_id:
                            from ..analytics.database import get_db_connection

                            conn = get_db_connection()
                            try:
                                conn.execute(
                                    """
                                    UPDATE meal_plans
                                    SET times_ordered = times_ordered + 1,
                                        last_ordered_at = ?
                                    WHERE id = ? AND user_id = ?
                                    """,
                                    (datetime.now().isoformat(), plan_id, user_id),
                                )
                                conn.commit()
                            finally:
                                conn.close()
                    except Exception as tracking_err:
                        if ctx:
                            ctx.error(f"Local tracking failed after Kroger order: {tracking_err}")
                        local_tracking_warning = str(tracking_err)

                    result = {
                        "success": True,
                        "message": f"Added {len(api_items)} items to cart",
                        "items_ordered": [
                            {
                                "name": item["name"],
                                "quantity": max(1, int(round(item.get("quantity", 1)))),
                                "product_id": item["product_id"],
                            }
                            for item in items_to_add
                            if item.get("product_id")
                        ],
                        "items_skipped": [i["name"] for i in items_to_skip],
                        "items_unknown": [i["name"] for i in items_unknown],
                        "modality": mod,
                        "date_range": shopping.get("date_range"),
                        "recipes_covered": [
                            r["recipe_name"] for r in shopping.get("recipes_included", [])
                        ],
                        "reminder": (
                            "Please review your cart in the Kroger app before checkout. "
                            "Would you like to update any pantry levels?"
                        ),
                    }
                    if local_tracking_warning:
                        result["local_tracking_warning"] = (
                            f"Kroger order succeeded, but local tracking failed: "
                            f"{local_tracking_warning}"
                        )
                    return result

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
                        "error": f"Failed to add to cart: {error_msg}",
                        "items_attempted": len(items_to_add),
                    }

            case "get_week_view":
                return meal_planning.get_week_view(start_date=week_start_date, user_id=user_id)

            case "get_summary":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}

                return meal_planning.get_meal_plan_summary(plan_id=plan_id, user_id=user_id)

            case "generate_draft":
                result = meal_planning.generate_draft(user_id=user_id)
                if ctx and result.get("success") and result.get("is_draft"):
                    ctx.info(result.get("message", "Draft generated"))
                return result

            case "approve_draft":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}
                return meal_planning.approve_draft(plan_id=plan_id, user_id=user_id)

            case "skip_meal":
                if not plan_id or not meal_date or not meal_slot:
                    return {
                        "success": False,
                        "error": "plan_id, meal_date and meal_slot are required",
                    }
                return meal_planning.skip_pending_meal(
                    plan_id=plan_id,
                    meal_date=meal_date,
                    meal_slot=meal_slot,
                    user_id=user_id,
                )

            case "undo_cooked":
                if not plan_id or not meal_date or not meal_slot:
                    return {
                        "success": False,
                        "error": "plan_id, meal_date and meal_slot are required",
                    }
                return meal_planning.undo_meal_cooked(
                    plan_id=plan_id,
                    meal_date=meal_date,
                    meal_slot=meal_slot,
                    user_id=user_id,
                )

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
