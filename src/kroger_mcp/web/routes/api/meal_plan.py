"""API routes for meal plan CRUD, meal assignment, cook tracking, and shopping."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.auth.dependencies import current_user_id

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CreatePlanBody(BaseModel):
    name: str
    start_date: str
    plan_type: str = "weekly"
    description: str | None = None
    is_template: bool = False


class AddMealBody(BaseModel):
    recipe_id: str
    meal_date: str
    meal_slot: str = "dinner"


class SwapMealsBody(BaseModel):
    date1: str
    slot1: str
    date2: str
    slot2: str


class CopyPlanBody(BaseModel):
    new_name: str
    new_start_date: str


class FromTemplateBody(BaseModel):
    source_plan_id: str
    new_name: str
    new_start_date: str


class ActualIngredient(BaseModel):
    product_id: str
    name: str = ""
    quantity: float
    unit: str = ""


class MarkCookedBody(BaseModel):
    cooked: bool = True
    deduct: bool = True
    actuals: list[ActualIngredient] | None = None


class ToggleTemplateBody(BaseModel):
    is_template: bool


class AddToCartBody(BaseModel):
    modality: str = "PICKUP"


class ScheduleRecipeBody(BaseModel):
    recipe_id: str
    meal_date: str
    meal_slot: Literal["breakfast", "lunch", "dinner", "snack"]


# ---------------------------------------------------------------------------
# Existing endpoints — now routing through service layer
# ---------------------------------------------------------------------------


@router.post("/api/meal-plan")
async def create_meal_plan(body: CreatePlanBody, request: Request):
    """Create a new meal plan owned by the current user."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import create_meal_plan as _create

        result = _create(
            name=body.name,
            start_date=body.start_date,
            plan_type=body.plan_type,
            description=body.description,
            is_template=body.is_template,
            user_id=user_id,
        )
        if isinstance(result, dict) and not result.get("success", True):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/meal-plan/{plan_id}")
async def delete_meal_plan(plan_id: str, request: Request):
    """Delete a meal plan and its entries."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import delete_meal_plan as _delete

        result = _delete(plan_id, user_id=user_id)
        if not result.get("success"):
            return JSONResponse(status_code=404, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/meal-plan/{plan_id}/meals")
async def add_meal_to_plan(plan_id: str, body: AddMealBody, request: Request):
    """Assign a recipe to a meal slot in a plan."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import assign_meal

        result = assign_meal(
            plan_id=plan_id,
            recipe_id=body.recipe_id,
            meal_date=body.meal_date,
            meal_slot=body.meal_slot,
            user_id=user_id,
        )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


def _ensure_week_plan(meal_dt: datetime, user_id: str) -> tuple[str, bool]:
    """Return (plan_id, created) for the plan covering meal_dt.

    Reuses any non-template plan whose range already contains the date;
    otherwise creates a Monday-aligned weekly plan for that week so it renders
    correctly in the Monday-aligned week view. Raises on creation failure.
    """
    from kroger_mcp.analytics.meal_planning import (
        create_meal_plan,
        find_plan_covering_date,
    )

    meal_date = meal_dt.strftime("%Y-%m-%d")
    existing = find_plan_covering_date(meal_date, user_id=user_id)
    if existing:
        return existing["id"], False

    monday = meal_dt - timedelta(days=meal_dt.weekday())
    sunday = monday + timedelta(days=6)
    created = create_meal_plan(
        name=f"Week of {monday.strftime('%b %d')}",
        start_date=monday.strftime("%Y-%m-%d"),
        end_date=sunday.strftime("%Y-%m-%d"),
        plan_type="weekly",
        user_id=user_id,
    )
    if not created.get("success"):
        raise RuntimeError(created.get("error", "Failed to create meal plan"))
    return created["plan_id"], True


def _displaced_recipe_name(
    plan_id: str, meal_date: str, meal_slot: str, new_recipe_id: str, user_id: str
) -> str | None:
    """Name of the recipe currently in this slot if a *different* one would be
    overwritten, else None. assign_meal silently INSERT OR REPLACEs, so this is
    the only way to warn the user about a swap."""
    from kroger_mcp.analytics.database import get_db_cursor
    from kroger_mcp.analytics.meal_planning import get_recipe

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT recipe_id FROM meal_entries "
            "WHERE plan_id = ? AND meal_date = ? AND meal_slot = ? AND user_id = ?",
            (plan_id, meal_date, meal_slot, user_id),
        )
        row = cursor.fetchone()

    if not row or row[0] == new_recipe_id:
        return None
    displaced = get_recipe(row[0])
    return displaced.get("name") if displaced else row[0]


@router.post("/api/meal-plan/schedule-recipe")
async def schedule_recipe(body: ScheduleRecipeBody, request: Request):
    """Schedule a recipe into the user's meal plan, creating the week's plan if
    none exists. Powers the 'when will you make this?' popup shown after a
    recipe is added to the shopping list."""
    user_id = current_user_id(request)
    logger.info(
        "schedule_recipe recipe=%s date=%s slot=%s user=%s",
        body.recipe_id,
        body.meal_date,
        body.meal_slot,
        user_id,
    )
    try:
        try:
            meal_dt = datetime.strptime(body.meal_date, "%Y-%m-%d")
        except ValueError:
            logger.warning("schedule_recipe bad date=%s", body.meal_date)
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid meal_date format. Use YYYY-MM-DD"},
            )

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if meal_dt <= today:
            return JSONResponse(
                status_code=400,
                content={"error": "Pick a date after today."},
            )

        plan_id, created_plan = _ensure_week_plan(meal_dt, user_id)
        displaced = _displaced_recipe_name(
            plan_id, body.meal_date, body.meal_slot, body.recipe_id, user_id
        )

        from kroger_mcp.analytics.meal_planning import assign_meal

        result = assign_meal(
            plan_id=plan_id,
            recipe_id=body.recipe_id,
            meal_date=body.meal_date,
            meal_slot=body.meal_slot,
            user_id=user_id,
        )
        if not result.get("success"):
            logger.warning("schedule_recipe assign failed: %s", result.get("error"))
            return JSONResponse(status_code=400, content=result)

        return {
            "success": True,
            "plan_id": plan_id,
            "meal_date": body.meal_date,
            "meal_slot": body.meal_slot,
            "recipe_name": result.get("recipe_name"),
            "created_plan": created_plan,
            "overwrote": displaced is not None,
            "displaced_recipe_name": displaced,
        }
    except Exception as exc:
        logger.exception("schedule_recipe failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/meal-plan/{plan_id}/meals")
async def remove_meal_from_plan(plan_id: str, meal_date: str, meal_slot: str, request: Request):
    """Remove a recipe from a specific meal slot."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.database import ensure_initialized, get_db_cursor

        ensure_initialized()
        with get_db_cursor() as cursor:
            cursor.execute(
                "DELETE FROM meal_entries "
                "WHERE plan_id = ? AND meal_date = ? AND meal_slot = ? AND user_id = ?",
                (plan_id, meal_date, meal_slot, user_id),
            )
            if cursor.rowcount == 0:
                return JSONResponse(
                    status_code=404,
                    content={"error": "No meal found for that date and slot"},
                )
        return {"success": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ---------------------------------------------------------------------------
# New endpoints
# ---------------------------------------------------------------------------


@router.get("/api/meal-plan/list")
async def list_plans(
    request: Request,
    include_templates: bool = Query(False),
    limit: int = Query(50),
):
    """List all meal plans (non-template by default) for the current user."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import list_plans_for_api

        return list_plans_for_api(include_templates=include_templates, limit=limit, user_id=user_id)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/meal-plan/templates")
async def list_templates(request: Request):
    """List all template plans for the current user."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import list_plans_for_api

        result = list_plans_for_api(include_templates=True, limit=50, user_id=user_id)
        if not result.get("success"):
            return JSONResponse(status_code=500, content=result)
        templates = [p for p in result["plans"] if p.get("is_template")]
        return {"success": True, "plans": templates}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/meal-plan/from-template")
async def create_from_template(body: FromTemplateBody, request: Request):
    """Create a new plan from a template."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import copy_meal_plan

        result = copy_meal_plan(
            source_plan_id=body.source_plan_id,
            new_name=body.new_name,
            new_start_date=body.new_start_date,
            user_id=user_id,
        )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/meal-plan/{plan_id}/copy")
async def copy_plan(plan_id: str, body: CopyPlanBody, request: Request):
    """Copy a meal plan to a new date range."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import copy_meal_plan

        result = copy_meal_plan(
            source_plan_id=plan_id,
            new_name=body.new_name,
            new_start_date=body.new_start_date,
            user_id=user_id,
        )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.patch("/api/meal-plan/{plan_id}/template")
async def toggle_template(plan_id: str, body: ToggleTemplateBody, request: Request):
    """Toggle is_template flag on a plan, only if the current user owns it."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.database import ensure_initialized, get_db_cursor

        ensure_initialized()
        with get_db_cursor() as cursor:
            cursor.execute(
                "UPDATE meal_plans SET is_template = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (1 if body.is_template else 0, datetime.now().isoformat(), plan_id, user_id),
            )
            if cursor.rowcount == 0:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"Plan '{plan_id}' not found"},
                )
        return {"success": True, "plan_id": plan_id, "is_template": body.is_template}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/meal-plan/{plan_id}/meals/swap")
async def swap_meals(plan_id: str, body: SwapMealsBody, request: Request):
    """Swap two meal slots within a plan."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import swap_meals as _swap

        result = _swap(
            plan_id=plan_id,
            date1=body.date1,
            slot1=body.slot1,
            date2=body.date2,
            slot2=body.slot2,
            user_id=user_id,
        )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/meal-plan/{plan_id}/meals/{meal_date}/{meal_slot}")
async def remove_meal(plan_id: str, meal_date: str, meal_slot: str, request: Request):
    """Remove a meal from a plan slot."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import remove_meal as _remove

        result = _remove(
            plan_id=plan_id,
            meal_date=meal_date,
            meal_slot=meal_slot,
            user_id=user_id,
        )
        if not result.get("success"):
            return JSONResponse(status_code=404, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.patch("/api/meal-plan/{plan_id}/meals/{meal_date}/{meal_slot}/cooked")
async def mark_meal_cooked(
    plan_id: str,
    meal_date: str,
    meal_slot: str,
    body: MarkCookedBody,
    request: Request,
):
    """Mark a meal cooked (deducting actual amounts from the pantry) or unmark
    it (reversing that deduction exactly)."""
    user_id = current_user_id(request)
    try:
        if body.cooked:
            from kroger_mcp.analytics.meal_planning import mark_meal_cooked as _mark

            actuals = [a.model_dump() for a in body.actuals] if body.actuals else None
            result = _mark(
                plan_id=plan_id,
                meal_date=meal_date,
                meal_slot=meal_slot,
                deduct_pantry=body.deduct,
                user_id=user_id,
                actuals=actuals,
            )
        else:
            # Unmarking reverses the pantry deduction exactly (the prior bare
            # cooked_at=NULL left the pantry drained).
            from kroger_mcp.analytics.meal_planning import undo_meal_cooked

            result = undo_meal_cooked(
                plan_id=plan_id,
                meal_date=meal_date,
                meal_slot=meal_slot,
                user_id=user_id,
            )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/meal-plan/{plan_id}/meals/{meal_date}/{meal_slot}/cook-preview")
async def cook_preview(plan_id: str, meal_date: str, meal_slot: str, request: Request):
    """Prefill data for the cook popup of a scheduled meal: scaled ingredient
    amounts + current pantry levels. Deducts nothing."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import preview_meal_cook

        result = preview_meal_cook(plan_id, meal_date, meal_slot, user_id=user_id)
        if not result.get("success"):
            return JSONResponse(status_code=404, content=result)
        return result
    except Exception as exc:
        logger.exception("cook_preview failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/meal-plan/{plan_id}/shopping-preview")
async def shopping_preview(plan_id: str, request: Request):
    """Preview shopping list for a plan (no cart action)."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import generate_meal_plan_shopping_list

        result = generate_meal_plan_shopping_list(plan_id=plan_id, user_id=user_id)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/meal-plan/{plan_id}/add-to-cart")
async def add_plan_to_cart(plan_id: str, body: AddToCartBody, request: Request):
    """Add all plan ingredients to cart (requires Kroger auth)."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import generate_meal_plan_shopping_list
        from kroger_mcp.tools.cart_tools import _add_item_to_local_cart
        from kroger_mcp.tools.shared import get_authenticated_client

        shopping = generate_meal_plan_shopping_list(plan_id=plan_id, user_id=user_id)
        if not shopping.get("success"):
            return JSONResponse(status_code=400, content=shopping)

        items_to_add = shopping.get("items_to_add", [])
        if not items_to_add:
            return {
                "success": True,
                "message": "No items to add — all ingredients are well-stocked or unlinked",
                "items_ordered": [],
                "items_skipped": [i["name"] for i in shopping.get("items_to_skip", [])],
            }

        mod = body.modality or "PICKUP"
        try:
            client = await asyncio.to_thread(get_authenticated_client, user_id)
        except Exception:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Not authenticated with Kroger. "
                    "Use auth(action='start') in Claude to connect your account.",
                    "auth_required": True,
                },
            )

        api_items = [
            {
                "upc": item["product_id"],
                "quantity": max(1, int(round(item.get("quantity", 1)))),
                "modality": mod,
            }
            for item in items_to_add
            if item.get("product_id")
        ]

        # Add with per-item fallback on 400
        failed_upcs: list = []
        added_api_items = list(api_items)
        try:
            await asyncio.to_thread(client.cart.add_to_cart, api_items)
        except Exception as batch_err:
            batch_err_str = str(batch_err)
            is_400 = "400" in batch_err_str or "Bad Request" in batch_err_str
            if "401" in batch_err_str or "Unauthorized" in batch_err_str:
                raise
            if is_400 and len(api_items) > 1:
                added_api_items = []
                for api_item in api_items:
                    try:
                        await asyncio.to_thread(client.cart.add_to_cart, [api_item])
                        added_api_items.append(api_item)
                    except Exception:
                        failed_upcs.append(api_item["upc"])
            else:
                raise

        added_upcs = {it["upc"] for it in added_api_items}

        for item in items_to_add:
            if item.get("product_id") and item["product_id"] in added_upcs:
                _add_item_to_local_cart(
                    item["product_id"],
                    max(1, int(round(item.get("quantity", 1)))),
                    mod,
                )

        result = {
            "success": True,
            "message": f"Added {len(added_api_items)} items to cart",
            "modality": mod,
            "items_ordered": [
                i["name"]
                for i in items_to_add
                if i.get("product_id") and i["product_id"] in added_upcs
            ],
            "items_skipped": [i["name"] for i in shopping.get("items_to_skip", [])],
        }
        if failed_upcs:
            result["items_failed"] = len(failed_upcs)
            result["warning"] = (
                f"{len(failed_upcs)} item(s) rejected by Kroger API "
                "(invalid product ID or not available at this location)"
            )
        return result
    except Exception as exc:
        err = str(exc)
        if "401" in err or "Unauthorized" in err:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Kroger authentication expired. Re-authenticate in Claude.",
                    "auth_required": True,
                },
            )
        return JSONResponse(status_code=500, content={"error": err})


@router.get("/api/meal-plan/{plan_id}/summary")
async def plan_summary(plan_id: str, request: Request):
    """Lightweight stats: meal_count, unique_recipes, cooked_count."""
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import get_plan_summary_stats

        return get_plan_summary_stats(plan_id, user_id=user_id)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/meal-plan/{plan_id}/week")
async def plan_week_view(
    plan_id: str,
    request: Request,
    week_offset: int = Query(0, description="Weeks offset from current week"),
):
    """
    Return a week grid for the given plan, offset from the current Monday.

    Response shape:
      {week_label, week_start, days: [{date, day_short, slots: {breakfast,lunch,dinner,snack}}]}
    """
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.meal_planning import (
            get_meal_entries_for_dates,
            get_recipe,
            reconcile_past_meals,
        )

        # Viewing the plan is the lazy trigger: auto-deduct any meals whose date
        # has passed so the grid (and pantry levels behind it) stay accurate.
        reconcile_past_meals(user_id=user_id, plan_id=plan_id)

        today = datetime.now()
        days_since_monday = today.weekday()
        monday = today - timedelta(days=days_since_monday) + timedelta(weeks=week_offset)
        sunday = monday + timedelta(days=6)

        week_start_str = monday.strftime("%Y-%m-%d")
        week_end_str = sunday.strftime("%Y-%m-%d")

        start_label = monday.strftime("%b %-d")
        end_label = sunday.strftime("%b %-d, %Y")
        week_label = f"{start_label} – {end_label}"

        entries = get_meal_entries_for_dates(
            plan_id=plan_id,
            start_date=week_start_str,
            end_date=week_end_str,
            user_id=user_id,
        )

        # Build lookup {(date, slot): {recipe_name, recipe_id, cooked_at}}
        slot_map: dict = {}
        for e in entries:
            recipe = get_recipe(e["recipe_id"])
            slot_map[(e["meal_date"], e["meal_slot"])] = {
                "recipe_id": e["recipe_id"],
                "recipe_name": recipe.get("name") if recipe else e["recipe_id"],
                "cooked_at": e.get("cooked_at"),
            }

        day_shorts = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        slots = ["breakfast", "lunch", "dinner", "snack"]
        days = []
        for i in range(7):
            day_dt = monday + timedelta(days=i)
            day_str = day_dt.strftime("%Y-%m-%d")
            day_slots = {}
            for slot in slots:
                day_slots[slot] = slot_map.get((day_str, slot))
            days.append(
                {
                    "date": day_str,
                    "day_short": day_shorts[i],
                    "slots": day_slots,
                }
            )

        return {
            "success": True,
            "plan_id": plan_id,
            "week_label": week_label,
            "week_start": week_start_str,
            "days": days,
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
