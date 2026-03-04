"""
Meal planner tools for creating and managing meal plans.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastmcp import Context
from pydantic import Field

from .shared import get_authenticated_client
from ..analytics import meal_planning


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
            "preview_shopping",
            "add_to_cart",
            "get_week_view",
            "get_summary",
        ] = Field(
            description=(
                "Action: 'create' - create a new meal plan, "
                "'list' - list all meal plans, "
                "'get' - get full details of a meal plan, "
                "'update' - update meal plan metadata, "
                "'delete' - delete a meal plan, "
                "'copy' - copy a plan to a new date range, "
                "'assign_meal' - assign recipe(s) to meal slot(s), "
                "'remove_meal' - remove a recipe from a meal slot, "
                "'swap' - swap two meal assignments, "
                "'preview_shopping' - preview shopping list for meal plan, "
                "'add_to_cart' - add meal plan ingredients to cart (2-step: preview then confirm), "
                "'get_week_view' - get calendar view of meals for a week, "
                "'get_summary' - get summary statistics for a meal plan"
            )
        ),
        plan_id: Optional[str] = Field(
            default=None,
            description="Plan identifier (for get, update, delete, assign_meal, remove_meal, swap, preview_shopping, add_to_cart, get_summary)",
        ),
        name: Optional[str] = Field(
            default=None,
            description="Plan name e.g. 'Week of Jan 27' (for create, update, copy)",
        ),
        start_date: Optional[str] = Field(
            default=None,
            description="Start date YYYY-MM-DD (for create, update, preview_shopping, add_to_cart)",
        ),
        end_date: Optional[str] = Field(
            default=None,
            description="End date YYYY-MM-DD (for create, update, preview_shopping, add_to_cart)",
        ),
        plan_type: Optional[str] = Field(
            default="weekly",
            description="Plan type: 'weekly', 'monthly', or 'custom' (for create)",
        ),
        description: Optional[str] = Field(
            default=None,
            description="Optional description (for create, update)",
        ),
        is_template: Optional[bool] = Field(
            default=False,
            description="Save as reusable template (for create)",
        ),
        include_past: Optional[bool] = Field(
            default=False,
            description="Include plans with past end dates (for list)",
        ),
        include_templates: Optional[bool] = Field(
            default=False,
            description="Include template plans (for list)",
        ),
        limit: Optional[int] = Field(
            default=20,
            description="Maximum plans to return (for list)",
        ),
        include_recipe_details: Optional[bool] = Field(
            default=True,
            description="Include full recipe names and servings (for get)",
        ),
        source_plan_id: Optional[str] = Field(
            default=None,
            description="Plan to copy from (for copy)",
        ),
        new_name: Optional[str] = Field(
            default=None,
            description="Name for the copied plan (for copy)",
        ),
        new_start_date: Optional[str] = Field(
            default=None,
            description="Start date for the copied plan YYYY-MM-DD (for copy)",
        ),
        recipe_id: Optional[str] = Field(
            default=None,
            description="Recipe to assign (for assign_meal single mode)",
        ),
        meal_date: Optional[str] = Field(
            default=None,
            description="Date YYYY-MM-DD (for assign_meal, remove_meal)",
        ),
        meal_slot: Optional[str] = Field(
            default=None,
            description="Meal slot: 'breakfast', 'lunch', 'dinner', 'snack' (for assign_meal, remove_meal)",
        ),
        servings_override: Optional[int] = Field(
            default=None,
            description="Override recipe default servings (for assign_meal single mode)",
        ),
        notes: Optional[str] = Field(
            default=None,
            description="Optional notes (for assign_meal single mode)",
        ),
        assignments: Optional[List[Dict[str, Any]]] = Field(
            default=None,
            description="Batch assign_meal: list of {recipe_id, meal_date, meal_slot, servings_override, notes} (max 100)",
        ),
        date1: Optional[str] = Field(
            default=None,
            description="First date YYYY-MM-DD (for swap)",
        ),
        slot1: Optional[str] = Field(
            default=None,
            description="First slot (for swap)",
        ),
        date2: Optional[str] = Field(
            default=None,
            description="Second date YYYY-MM-DD (for swap)",
        ),
        slot2: Optional[str] = Field(
            default=None,
            description="Second slot (for swap)",
        ),
        days_ahead: Optional[int] = Field(
            default=None,
            description="Number of days from today to include (for preview_shopping, add_to_cart)",
        ),
        pantry_threshold: Optional[int] = Field(
            default=30,
            description="Skip items with pantry level above this % (for preview_shopping, add_to_cart)",
        ),
        combine_duplicates: Optional[bool] = Field(
            default=True,
            description="Merge same ingredients across recipes (for preview_shopping)",
        ),
        skip_items: Optional[List[str]] = Field(
            default=None,
            description="Ingredient names to skip (for preview_shopping, add_to_cart)",
        ),
        modality: Optional[str] = Field(
            default="PICKUP",
            description="Fulfillment method: PICKUP or DELIVERY (for add_to_cart)",
        ),
        confirm: Optional[bool] = Field(
            default=False,
            description="Set True to actually add items after preview (for add_to_cart)",
        ),
        week_start_date: Optional[str] = Field(
            default=None,
            description="Monday of the week YYYY-MM-DD, defaults to current week (for get_week_view)",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Meal plan management and shopping operations."""
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
                )

                if ctx and result.get("success"):
                    await ctx.info(f"Created meal plan '{name}'")

                return result

            case "list":
                return meal_planning.get_meal_plans(
                    include_past=include_past or False,
                    include_templates=include_templates or False,
                    limit=limit or 20,
                )

            case "get":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}

                return meal_planning.get_meal_plan(
                    plan_id=plan_id,
                    include_recipe_details=include_recipe_details if include_recipe_details is not None else True,
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
                )

            case "delete":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}

                result = meal_planning.delete_meal_plan(plan_id)

                if ctx and result.get("success"):
                    await ctx.info("Deleted meal plan")

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
                )

                if ctx and result.get("success"):
                    await ctx.info(f"Copied plan with {result.get('meals_copied', 0)} meals")

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
                        plan_id=plan_id, assignments=assignments
                    )

                    if ctx and result.get("success"):
                        await ctx.info(f"Assigned {result.get('assigned', 0)} meals")

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
                )

                if ctx and result.get("success"):
                    await ctx.info(
                        f"Assigned '{result.get('recipe_name')}' to {meal_slot}"
                    )

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
                )

            case "preview_shopping":
                return meal_planning.generate_meal_plan_shopping_list(
                    plan_id=plan_id,
                    start_date=start_date,
                    end_date=end_date,
                    days_ahead=days_ahead,
                    pantry_threshold=pantry_threshold if pantry_threshold is not None else 30,
                    combine_duplicates=combine_duplicates if combine_duplicates is not None else True,
                    skip_items=skip_items,
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
                    await ctx.info(f"Adding {len(items_to_add)} items to cart...")

                try:
                    client = get_authenticated_client()

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

                    client.cart.add_to_cart(api_items)

                    from .cart_tools import _add_item_to_local_cart

                    for item in items_to_add:
                        if item.get("product_id"):
                            _add_item_to_local_cart(
                                item["product_id"],
                                max(1, int(round(item.get("quantity", 1)))),
                                mod,
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
                                WHERE id = ?
                                """,
                                (datetime.now().isoformat(), plan_id),
                            )
                            conn.commit()
                        finally:
                            conn.close()

                    return {
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
                            r["recipe_name"]
                            for r in shopping.get("recipes_included", [])
                        ],
                        "reminder": (
                            "Please review your cart in the Kroger app before checkout. "
                            "Would you like to update any pantry levels?"
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
                        "error": f"Failed to add to cart: {error_msg}",
                        "items_attempted": len(items_to_add),
                    }

            case "get_week_view":
                return meal_planning.get_week_view(start_date=week_start_date)

            case "get_summary":
                if not plan_id:
                    return {"success": False, "error": "plan_id is required"}

                return meal_planning.get_meal_plan_summary(plan_id=plan_id)

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
