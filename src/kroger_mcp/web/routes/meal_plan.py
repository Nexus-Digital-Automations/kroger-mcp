"""Meal plan route — calendar grid view."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.database import ensure_initialized, get_db_connection
from kroger_mcp.tools.recipe_tools import _load_recipes

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

SLOTS = ["breakfast", "lunch", "dinner", "snack"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _get_all_plans():
    ensure_initialized()
    conn = get_db_connection()
    try:
        cursor = conn.execute("""
            SELECT id, name, description, start_date, end_date,
                   plan_type, is_template, times_ordered, last_ordered_at
            FROM meal_plans
            WHERE is_template = 0
            ORDER BY start_date DESC
        """)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def _get_plan_entries(plan_id: str):
    ensure_initialized()
    conn = get_db_connection()
    try:
        cursor = conn.execute("""
            SELECT recipe_id, meal_date, meal_slot
            FROM meal_entries
            WHERE plan_id = ?
            ORDER BY meal_date, meal_slot
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def _build_calendar(plan, entries, recipe_map, week_offset: int = 0):
    """Build a Mon–Sun grid for the plan, offset by week_offset weeks."""
    if not plan:
        return [], None, None

    start_dt = datetime.strptime(plan["start_date"], "%Y-%m-%d").date()
    end_dt = datetime.strptime(plan["end_date"], "%Y-%m-%d").date()

    # Find Monday of the first week, then apply offset
    first_monday = start_dt - timedelta(days=start_dt.weekday())
    view_monday = first_monday + timedelta(weeks=week_offset)
    view_sunday = view_monday + timedelta(days=6)

    # Clamp: don't go before plan start or after plan end
    view_monday = max(view_monday, start_dt - timedelta(days=start_dt.weekday()))
    view_sunday = view_monday + timedelta(days=6)

    week_dates = [view_monday + timedelta(days=i) for i in range(7)]

    # Build lookup: {(date_str, slot): recipe_name}
    entry_map = {}
    for e in entries:
        key = (e["meal_date"], e["meal_slot"])
        entry_map[key] = recipe_map.get(e["recipe_id"], e["recipe_id"])

    calendar = []
    for slot in SLOTS:
        row = {"slot": slot, "cells": []}
        for day in week_dates:
            date_str = day.isoformat()
            recipe_name = entry_map.get((date_str, slot))
            row["cells"].append({
                "date": day,
                "date_str": date_str,
                "recipe_name": recipe_name,
            })
        calendar.append(row)

    return calendar, week_dates, (view_monday, view_sunday)


@router.get("/meal-plan", response_class=HTMLResponse)
async def meal_plan_page(request: Request, plan_id: Optional[str] = None, week: int = 0):
    plans = _get_all_plans()

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

    if active_plan:
        entries = _get_plan_entries(active_plan["id"])
        total_meals = len(entries)
        unique_recipes = {e["recipe_id"] for e in entries}
        calendar, week_dates, _ = _build_calendar(active_plan, entries, recipe_map, week)

    today = datetime.now().date()

    recipes = recipe_data.get("recipes", [])

    return templates.TemplateResponse("meal_plan.html", {
        "request": request,
        "active_page": "meal_plan",
        "plans": plans,
        "active_plan": active_plan,
        "calendar": calendar,
        "week_dates": week_dates,
        "today": today,
        "week_offset": week,
        "total_meals": total_meals,
        "unique_recipe_count": len(unique_recipes),
        "slots": SLOTS,
        "recipes": recipes,
    })
