"""Dashboard route — aggregates key stats for the home page, scoped per user.

@stable
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_connection,
    run_in_thread,
)
from kroger_mcp.analytics.favorites import get_lists
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.tools.recipe_tools import _load_recipes
from kroger_mcp.web.templating import templates

router = APIRouter()


def _get_this_week_meals(user_id: str, recipe_map: dict[str, str]):
    """Return this user's meal entries for the current Mon–Sun week."""
    ensure_initialized()
    conn = get_db_connection()
    try:
        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)

        cursor = conn.execute(
            """
            SELECT me.meal_date, me.meal_slot, me.recipe_id,
                   mp.name as plan_name
            FROM meal_entries me
            JOIN meal_plans mp ON me.plan_id = mp.id
            WHERE me.meal_date BETWEEN ? AND ?
              AND me.user_id = ?
            ORDER BY me.meal_date, me.meal_slot
        """,
            (monday.isoformat(), sunday.isoformat(), user_id),
        )

        rows = cursor.fetchall()

        meals = []
        for row in rows:
            meals.append(
                {
                    "date": row["meal_date"],
                    "slot": row["meal_slot"],
                    "recipe_name": recipe_map.get(row["recipe_id"], row["recipe_id"]),
                }
            )
        return meals
    finally:
        conn.close()


def _get_meal_plan_count(user_id: str):
    ensure_initialized()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM meal_plans " "WHERE is_template = 0 AND user_id = ?",
            (user_id,),
        )
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
            overdue.append(
                {
                    "name": lst["name"],
                    "list_id": lst["id"],
                    "days_overdue": days_overdue,
                    "status": rs.get("status"),
                }
            )
    return overdue


def _get_smart_suggestions(user_id: str, limit: int = 6):
    """Overdue repurchase + upcoming-seasonal items, normalized to one shape
    for the dashboard card. Both are already computed by analytics.predictions
    (today only reachable via the MCP chat tool) -- this just surfaces them."""
    from kroger_mcp.analytics.predictions import get_shopping_suggestions

    suggestions = get_shopping_suggestions(user_id=user_id, days_ahead=14)

    items = [
        {
            "description": p["description"] or p["product_id"],
            "tag": f"{p['days_overdue']}d overdue" if p["days_overdue"] else "Overdue",
            "tag_kind": "danger",
        }
        for p in suggestions["overdue"][:limit]
    ]
    remaining = limit - len(items)
    if remaining > 0:
        items.extend(
            {
                "description": s["description"] or s["product_id"],
                "tag": s["holiday"] or "In season",
                "tag_kind": "info",
            }
            for s in suggestions["seasonal_items"][:remaining]
        )
    return items


def _dashboard_payload(user_id: str) -> dict:
    """All blocking work for the dashboard (JSON load + DB queries), run off
    the event loop via run_in_thread. Each helper opens/closes its own DB
    connection, so the whole payload is safe inside one worker thread.

    Pantry alerts, meals awaiting cook-confirmation, favorite-on-sale alerts,
    and the "plan next week" reminder are NOT computed here -- the
    `priorityFeed` Alpine component fetches those from GET /api/notifications
    (the same endpoint the bell polls) so the two never disagree. Only the
    two dashboard-only signals (overdue favorites, smart suggestions) are
    server-rendered, as JSON for that component's init args."""
    recipe_data = _load_recipes()
    recipes = recipe_data.get("recipes", [])
    recipe_map = {r["id"]: r["name"] for r in recipes}
    this_week_meals = _get_this_week_meals(user_id, recipe_map)
    meal_plan_count = _get_meal_plan_count(user_id)
    fav_lists = get_lists(user_id=user_id)
    overdue_favorites = _get_overdue_favorites(fav_lists)
    smart_suggestions = _get_smart_suggestions(user_id)

    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    week_days = [monday + timedelta(days=i) for i in range(7)]

    meals_by_date: dict[str, list[str]] = {}
    for meal in this_week_meals:
        date_str = meal["date"]
        if date_str not in meals_by_date:
            meals_by_date[date_str] = []
        meals_by_date[date_str].append(f"{meal['slot'].title()}: {meal['recipe_name']}")

    return {
        "active_page": "dashboard",
        "recipe_count": len(recipes),
        "meal_plan_count": meal_plan_count,
        "favorites_count": len(fav_lists),
        # First-run signal for the onboarding banner: every account auto-gets
        # a default "My Favorites" list AND a built-in "Snacks" list (list_type
        # 'snacks'), neither of which is_default alone captures — so count
        # only genuinely user-created lists.
        "custom_favorites_count": sum(
            1
            for lst in fav_lists
            if not lst.get("is_default") and lst.get("list_type") != "snacks"
        ),
        "week_days": week_days,
        "meals_by_date": meals_by_date,
        "today": today,
        "overdue_favorites": overdue_favorites,
        "smart_suggestions": smart_suggestions,
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    user_id = current_user_id(request)
    context = await run_in_thread(_dashboard_payload, user_id)
    return templates.TemplateResponse(request, "dashboard.html", context)
