"""Products search page route."""


from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kroger_mcp.analytics.database import ensure_initialized, get_db_connection
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.tools.shared import get_favorites_display_mode, get_preferred_location_id
from kroger_mcp.web.context import action_menu_context
from kroger_mcp.web.templating import templates

router = APIRouter()


@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    user_id = current_user_id(request)
    location_id = get_preferred_location_id(user_id=user_id) or "03400014"
    favorites_display_mode = get_favorites_display_mode(user_id=user_id)

    ensure_initialized()
    watchlist, favorite_ids = [], []
    try:
        conn = get_db_connection()
        watchlist = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM deal_watchlist WHERE user_id = ? ORDER BY added_at DESC",
                (user_id,),
            ).fetchall()
        ]
        favorite_ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT fli.product_id
                FROM favorite_list_items fli
                JOIN favorite_lists fl ON fli.list_id = fl.id
                WHERE fl.user_id = ?
                """,
                (user_id,),
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
            "favorites_display_mode": favorites_display_mode,
            **action_menu_context(user_id),
        },
    )
