"""Meal plan route — calendar grid view."""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_connection,
    run_in_thread,
)
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.tools.recipe_tools import _load_recipes
from kroger_mcp.web.templating import templates

router = APIRouter()

SLOTS = ["breakfast", "lunch", "dinner", "snack"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _get_all_plans(user_id: str, include_templates: bool = False):
    ensure_initialized()
    conn = get_db_connection()
    try:
        if include_templates:
            cursor = conn.execute(
                """
                SELECT id, name, description, start_date, end_date,
                       plan_type, is_template, times_ordered, last_ordered_at
                FROM meal_plans
                WHERE user_id = ?
                ORDER BY is_template ASC, start_date DESC
            """,
                (user_id,),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, name, description, start_date, end_date,
                       plan_type, is_template, times_ordered, last_ordered_at
                FROM meal_plans
                WHERE user_id = ? AND is_template = 0
                ORDER BY start_date DESC
            """,
                (user_id,),
            )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def _get_plan_entries(plan_id: str, user_id: str):
    ensure_initialized()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            SELECT recipe_id, meal_date, meal_slot, cooked_at
            FROM meal_entries
            WHERE plan_id = ? AND user_id = ?
            ORDER BY meal_date, meal_slot
        """,
            (plan_id, user_id),
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def _build_calendar(plan, entries, recipe_map, week_offset: int = 0):
    """Build a Mon–Sun grid for the plan, offset by week_offset weeks."""
    if not plan:
        return [], None, None

    start_dt = datetime.strptime(plan["start_date"], "%Y-%m-%d").date()

    # Find Monday of the first week, then apply offset
    first_monday = start_dt - timedelta(days=start_dt.weekday())
    view_monday = first_monday + timedelta(weeks=week_offset)

    view_sunday = view_monday + timedelta(days=6)

    week_dates = [view_monday + timedelta(days=i) for i in range(7)]

    # Build lookup: {(date_str, slot): {recipe_name, recipe_id, cooked_at}}
    entry_map = {}
    for e in entries:
        key = (e["meal_date"], e["meal_slot"])
        recipe_id = e["recipe_id"]
        entry_map[key] = {
            "recipe_name": recipe_map.get(recipe_id, recipe_id),
            "recipe_id": recipe_id,
            "cooked_at": e.get("cooked_at"),
        }

    calendar = []
    for slot in SLOTS:
        row: dict[str, Any] = {"slot": slot, "cells": []}
        for day in week_dates:
            date_str = day.isoformat()
            entry = entry_map.get((date_str, slot))
            row["cells"].append(
                {
                    "date": day,
                    "date_str": date_str,
                    "recipe_name": entry["recipe_name"] if entry else None,
                    "recipe_id": entry["recipe_id"] if entry else None,
                    "cooked_at": entry["cooked_at"] if entry else None,
                }
            )
        calendar.append(row)

    return calendar, week_dates, (view_monday, view_sunday)


def _meal_plan_payload(user_id: str, plan_id: str | None, week: int | None) -> dict:
    """All blocking work for the meal-plan page (DB queries + JSON load), run
    off the event loop via run_in_thread."""
    plans = _get_all_plans(user_id, include_templates=False)
    all_plans_with_templates = _get_all_plans(user_id, include_templates=True)
    templates_list = [p for p in all_plans_with_templates if p.get("is_template")]

    # Select active plan
    active_plan = None
    if plan_id:
        active_plan = next((p for p in plans if p["id"] == plan_id), None)
    if not active_plan and plans:
        active_plan = plans[0]

    # Resolve recipe names
    recipe_data = _load_recipes()
    recipe_map = {r["id"]: r["name"] for r in recipe_data.get("recipes", [])}

    entries = []
    calendar = []
    week_dates = []
    total_meals = 0
    unique_recipes = set()
    cooked_count = 0
    summary = {"meal_count": 0, "unique_recipes": 0, "cooked_count": 0}

    # week=None means first load — auto-advance to first week with entries.
    # week=<explicit int> means user navigated — honor it as-is.
    explicit_week = week is not None
    week_offset = week if week is not None else 0

    if active_plan:
        entries = _get_plan_entries(active_plan["id"], user_id)
        total_meals = len(entries)
        unique_recipes = {e["recipe_id"] for e in entries}
        cooked_count = sum(1 for e in entries if e.get("cooked_at"))
        summary = {
            "meal_count": total_meals,
            "unique_recipes": len(unique_recipes),
            "cooked_count": cooked_count,
        }

        # Auto-advance only on first load (no explicit week param)
        if entries and not explicit_week:
            start_dt = datetime.strptime(active_plan["start_date"], "%Y-%m-%d").date()
            first_monday = start_dt - timedelta(days=start_dt.weekday())
            entry_dates = [datetime.strptime(e["meal_date"], "%Y-%m-%d").date() for e in entries]
            earliest = min(entry_dates)
            default_week_end = first_monday + timedelta(days=6)
            if earliest > default_week_end:
                week_offset = (earliest - first_monday).days // 7

        calendar, week_dates, _ = _build_calendar(active_plan, entries, recipe_map, week_offset)

    today = datetime.now().date()
    recipes = recipe_data.get("recipes", [])

    return {
        "active_page": "meal_plan",
        "plans": plans,
        "templates_list": templates_list,
        "active_plan": active_plan,
        "calendar": calendar,
        "week_dates": week_dates,
        "today": today,
        "week_offset": week_offset,
        "total_meals": total_meals,
        "unique_recipe_count": len(unique_recipes),
        "cooked_count": cooked_count,
        "summary": summary,
        "slots": SLOTS,
        "recipes": recipes,
    }


@router.get("/meal-plan", response_class=HTMLResponse)
async def meal_plan_page(request: Request, plan_id: str | None = None, week: int | None = None):
    user_id = current_user_id(request)
    context = await run_in_thread(_meal_plan_payload, user_id, plan_id, week)
    return templates.TemplateResponse(request, "meal_plan.html", context)
