"""Meal Tracker route — log meals and track consumption."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.meal_tracker import get_today_meals
from kroger_mcp.analytics.pantry import get_pantry_status

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


@router.get("/meal-tracker", response_class=HTMLResponse)
async def meal_tracker_page(request: Request):
    pantry_items = get_pantry_status(apply_depletion=True)
    today_data = get_today_meals()

    return templates.TemplateResponse("meal_tracker.html", {
        "request": request,
        "active_page": "meal_tracker",
        "pantry_items": pantry_items,
        "today_meals": today_data.get("meals", []),
        "today_stats": today_data.get("stats", {}),
    })
