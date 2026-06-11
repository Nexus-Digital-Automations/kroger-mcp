"""Products search page route."""


from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kroger_mcp.analytics.database import ensure_initialized, get_db_connection
from kroger_mcp.tools.shared import get_preferred_location_id
from kroger_mcp.web.context import action_menu_context
from kroger_mcp.web.templating import templates

router = APIRouter()


@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    location_id = get_preferred_location_id() or "03400014"

    ensure_initialized()
    watchlist, favorite_ids = [], []
    try:
        conn = get_db_connection()
        watchlist = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM deal_watchlist ORDER BY added_at DESC"
            ).fetchall()
        ]
        favorite_ids = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT product_id FROM favorite_list_items"
            ).fetchall()
        ]
        conn.close()
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "active_page": "products",
            "location_id": location_id,
            "watchlist": watchlist,
            "watchlist_count": len(watchlist),
            "favorite_ids": favorite_ids,
            **action_menu_context(),
        },
    )
