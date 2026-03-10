"""Deals page route."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.database import get_db_connection, ensure_initialized

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/deals", response_class=HTMLResponse)
async def deals_page(request: Request):
    ensure_initialized()
    watchlist = []
    try:
        conn = get_db_connection()
        cursor = conn.execute(
            "SELECT * FROM deal_watchlist ORDER BY added_at DESC"
        )
        watchlist = [dict(row) for row in cursor.fetchall()]
        conn.close()
    except Exception:
        pass

    return templates.TemplateResponse("deals.html", {
        "request": request,
        "active_page": "deals",
        "watchlist": watchlist,
        "watchlist_count": len(watchlist),
    })
