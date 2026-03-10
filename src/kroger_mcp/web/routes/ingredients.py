"""Ingredient management page route."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.database import ensure_initialized

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/ingredients", response_class=HTMLResponse)
async def ingredients_page(request: Request):
    ensure_initialized()

    custom_ingredients = []
    try:
        from kroger_mcp.analytics.database import get_db_connection
        conn = get_db_connection()
        cursor = conn.execute(
            "SELECT * FROM custom_ingredients WHERE is_active = 1 ORDER BY ingredient_name"
        )
        custom_ingredients = [dict(row) for row in cursor.fetchall()]
        conn.close()
    except Exception:
        pass

    return templates.TemplateResponse("ingredients.html", {
        "request": request,
        "active_page": "ingredients",
        "custom_ingredients": custom_ingredients,
        "custom_count": len(custom_ingredients),
    })
