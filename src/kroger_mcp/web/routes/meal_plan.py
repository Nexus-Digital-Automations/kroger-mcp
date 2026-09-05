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
from kroger_mcp.tools.step_times import recipe_time_summary
from kroger_mcp.web.templating import templates

router = APIRouter()

SLOTS = ["breakfast", "lunch", "dinner", "snack"]


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
                WHERE user_id = ? AND is_draft = 0
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
                WHERE user_id = ? AND is_template = 0 AND is_draft = 0
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


def _build_calendar(
    plan, entries, recipe_map, week_offset: int = 0, time_map=None, week_start_day: int = 6
):
    """Build a 7-day grid for the plan, offset by week_offset weeks.

    The grid aligns to the week containing the plan's own start_date (using
    the user's configured week start day), not to "today's week".
    time_map: optional {recipe_id: "45 min"} labels shown on calendar cells.
    """
    from kroger_mcp.analytics.meal_planning import week_start_for_date

    time_map = time_map or {}
    if not plan:
        return [], None, None

    start_dt = datetime.strptime(plan["start_date"], "%Y-%m-%d").date()

    first_week_start = week_start_for_date(start_dt, week_start_day)
    view_start = first_week_start + timedelta(weeks=week_offset)

    view_end = view_start + timedelta(days=6)

    week_dates = [view_start + timedelta(days=i) for i in range(7)]

    # Build lookup: {(date_str, slot): {recipe_name, recipe_id, cooked_at}}
    entry_map = {}
    for e in entries:
        key = (e["meal_date"], e["meal_slot"])
        recipe_id = e["recipe_id"]
        entry_map[key] = {
            "recipe_name": recipe_map.get(recipe_id, recipe_id),
            "recipe_id": recipe_id,
            # Deleted recipes fall back to showing the raw id; linking one
            # would 404 on /recipes/{id}, so the template only links known ids.
            "recipe_known": recipe_id in recipe_map,
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
                    "recipe_known": entry["recipe_known"] if entry else False,
                    "cooked_at": entry["cooked_at"] if entry else None,
                    "time_label": time_map.get(entry["recipe_id"]) if entry else None,
                }
            )
        calendar.append(row)

    return calendar, week_dates, (view_start, view_end)


def _meal_plan_payload(user_id: str, plan_id: str | None, week: int | None) -> dict:
    """All blocking work for the meal-plan page (DB queries + JSON load), run
    off the event loop via run_in_thread."""
    from kroger_mcp.tools.shared import get_week_start_day

    week_start_day = get_week_start_day(user_id=user_id)
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
            from kroger_mcp.analytics.meal_planning import week_start_for_date

            start_dt = datetime.strptime(active_plan["start_date"], "%Y-%m-%d").date()
            first_week_start = week_start_for_date(start_dt, week_start_day)
            entry_dates = [datetime.strptime(e["meal_date"], "%Y-%m-%d").date() for e in entries]
            earliest = min(entry_dates)
            default_week_end = first_week_start + timedelta(days=6)
            if earliest > default_week_end:
                week_offset = (earliest - first_week_start).days // 7

        # Time labels only for recipes actually on the plan (cheap regex parse).
        plan_recipe_ids = {e["recipe_id"] for e in entries}
        time_map = {}
        for r in recipe_data.get("recipes", []):
            if r.get("id") in plan_recipe_ids:
                try:
                    # Distinct name: reusing `summary` here used to clobber the
                    # page's meal-count summary dict built above.
                    time_summary = recipe_time_summary(r)
                    if time_summary["total"]:
                        time_map[r["id"]] = time_summary["label"]
                except Exception:
                    pass

        calendar, week_dates, _ = _build_calendar(
            active_plan, entries, recipe_map, week_offset, time_map, week_start_day
        )

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
