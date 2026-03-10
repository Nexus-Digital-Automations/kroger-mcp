"""Dashboard route — aggregates key stats for the home page."""

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.database import ensure_initialized, get_db_connection
from kroger_mcp.analytics.favorites import get_lists
from kroger_mcp.tools.recipe_tools import _load_recipes

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


def _get_pantry_alerts():
    """Return pantry items that are low or expiring within 7 days."""
    ensure_initialized()
    conn = get_db_connection()
    try:
        cursor = conn.execute("""
            SELECT product_id, description, level_percent, low_threshold,
                   expiration_date, days_to_expiration
            FROM pantry_items
            ORDER BY level_percent ASC
        """)
        items = []
        now = datetime.now()
        for row in cursor.fetchall():
            item = dict(row)
            level = item["level_percent"]
            threshold = item["low_threshold"] or 20
            is_low = level <= threshold
            is_expiring = False
            days_to_exp = item.get("days_to_expiration")
            if days_to_exp is not None and days_to_exp <= 7:
                is_expiring = True
            if is_low or is_expiring:
                items.append({
                    "description": item["description"] or item["product_id"],
                    "level_percent": round(level),
                    "is_low": is_low,
                    "is_expiring": is_expiring,
                    "days_to_expiration": days_to_exp,
                })
        return items
    finally:
        conn.close()


def _get_this_week_meals():
    """Return meal entries for the current Mon–Sun week."""
    ensure_initialized()
    conn = get_db_connection()
    try:
        today = datetime.now().date()
        # Monday of this week
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)

        cursor = conn.execute("""
            SELECT me.meal_date, me.meal_slot, me.recipe_id,
                   mp.name as plan_name
            FROM meal_entries me
            JOIN meal_plans mp ON me.plan_id = mp.id
            WHERE me.meal_date BETWEEN ? AND ?
            ORDER BY me.meal_date, me.meal_slot
        """, (monday.isoformat(), sunday.isoformat()))

        rows = cursor.fetchall()
        # Resolve recipe names
        recipe_data = _load_recipes()
        recipe_map = {r["id"]: r["name"] for r in recipe_data.get("recipes", [])}

        meals = []
        for row in rows:
            meals.append({
                "date": row["meal_date"],
                "slot": row["meal_slot"],
                "recipe_name": recipe_map.get(row["recipe_id"], row["recipe_id"]),
            })
        return meals
    finally:
        conn.close()


def _get_meal_plan_count():
    ensure_initialized()
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT COUNT(*) as cnt FROM meal_plans WHERE is_template = 0")
        row = cursor.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def _get_overdue_favorites(lists):
    """Filter lists that are overdue for reorder."""
    overdue = []
    for lst in lists:
        rs = lst.get("reorder_status", {})
        if rs.get("is_overdue") and rs.get("has_schedule"):
            days_overdue = rs.get("days_overdue", 0)
            overdue.append({
                "name": lst["name"],
                "list_id": lst["id"],
                "days_overdue": days_overdue,
                "status": rs.get("status"),
            })
    return overdue


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    recipe_data = _load_recipes()
    recipes = recipe_data.get("recipes", [])
    pantry_alerts = _get_pantry_alerts()
    this_week_meals = _get_this_week_meals()
    meal_plan_count = _get_meal_plan_count()
    fav_lists = get_lists()
    overdue_favorites = _get_overdue_favorites(fav_lists)

    # Build week calendar strip
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    week_days = [monday + timedelta(days=i) for i in range(7)]

    meals_by_date = {}
    for meal in this_week_meals:
        date_str = meal["date"]
        if date_str not in meals_by_date:
            meals_by_date[date_str] = []
        meals_by_date[date_str].append(f"{meal['slot'].title()}: {meal['recipe_name']}")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "recipe_count": len(recipes),
        "pantry_alert_count": len(pantry_alerts),
        "meal_plan_count": meal_plan_count,
        "favorites_count": len(fav_lists),
        "pantry_alerts": pantry_alerts[:10],
        "week_days": week_days,
        "meals_by_date": meals_by_date,
        "today": today,
        "overdue_favorites": overdue_favorites,
    })
