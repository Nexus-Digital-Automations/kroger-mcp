"""Safety configuration page route."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.database import ensure_initialized
from kroger_mcp.analytics.ingredients import BAD_INGREDIENTS

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/safety", response_class=HTMLResponse)
async def safety_page(request: Request):
    ensure_initialized()

    settings = {"filtering_enabled": True, "block_mode": "soft"}
    safe_count = 0
    blocked_count = 0
    ingredient_count = 0

    try:
        from kroger_mcp.analytics.safety import get_safety_settings
        settings = get_safety_settings()
    except Exception:
        pass

    try:
        ingredient_count = len(BAD_INGREDIENTS)
    except Exception:
        pass

    try:
        from kroger_mcp.analytics.database import get_db_connection
        conn = get_db_connection()
        r1 = conn.execute("SELECT COUNT(*) as cnt FROM safe_products")
        safe_count = r1.fetchone()["cnt"]
        r2 = conn.execute("SELECT COUNT(*) as cnt FROM blocked_products")
        blocked_count = r2.fetchone()["cnt"]
        conn.close()
    except Exception:
        pass

    return templates.TemplateResponse("safety.html", {
        "request": request,
        "active_page": "safety",
        "settings": settings,
        "safe_count": safe_count,
        "blocked_count": blocked_count,
        "ingredient_count": ingredient_count,
    })
