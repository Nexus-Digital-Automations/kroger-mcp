"""Products search page route."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.database import get_db_connection, ensure_initialized
from kroger_mcp.tools.shared import get_preferred_location_id, get_product_sort_preferences
from kroger_mcp.web.context import action_menu_context

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    location_id = get_preferred_location_id() or "03400014"

    ensure_initialized()
    watchlist, favorite_ids = [], []
    try:
        conn = get_db_connection()
        watchlist = [dict(row) for row in conn.execute(
            "SELECT * FROM deal_watchlist ORDER BY added_at DESC"
        ).fetchall()]
        favorite_ids = [row[0] for row in conn.execute(
            "SELECT DISTINCT product_id FROM favorite_list_items"
        ).fetchall()]
        conn.close()
    except Exception:
        pass

    sort_prefs = get_product_sort_preferences()

    return templates.TemplateResponse("products.html", {
        "request": request,
        "active_page": "products",
        "location_id": location_id,
        "watchlist": watchlist,
        "watchlist_count": len(watchlist),
        "favorite_ids": favorite_ids,
        "sort_prefs": sort_prefs,
        **action_menu_context(),
    })
