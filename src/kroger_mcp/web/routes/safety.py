"""Safety configuration page route."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.analytics.database import ensure_initialized
from kroger_mcp.analytics.ingredients import BAD_INGREDIENTS
from kroger_mcp.auth.dependencies import current_user_id

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/safety", response_class=HTMLResponse)
async def safety_page(request: Request):
    ensure_initialized()
    user_id = current_user_id(request)

    settings = {"filtering_enabled": True, "block_mode": "soft"}
    safe_count = 0
    blocked_count = 0
    ingredient_count = 0

    try:
        from kroger_mcp.analytics.safety import get_safety_settings

        settings = get_safety_settings(user_id=user_id)
    except Exception:
        pass

    try:
        ingredient_count = len(BAD_INGREDIENTS)
    except Exception:
        pass

    custom_ingredients = []
    try:
        from kroger_mcp.analytics.database import get_db_connection

        conn = get_db_connection()
        r1 = conn.execute("SELECT COUNT(*) as cnt FROM safe_products WHERE user_id = ?", (user_id,))
        safe_count = r1.fetchone()["cnt"]
        r2 = conn.execute(
            "SELECT COUNT(*) as cnt FROM blocked_products WHERE user_id = ?", (user_id,)
        )
        blocked_count = r2.fetchone()["cnt"]
        conn.close()
    except Exception:
        pass

    try:
        from kroger_mcp.analytics.database import get_db_connection

        conn2 = get_db_connection()
        cursor = conn2.execute(
            """
            SELECT * FROM custom_ingredients
            WHERE is_active = 1 AND user_id = ?
            ORDER BY ingredient_name
            """,
            (user_id,),
        )
        custom_ingredients = [dict(row) for row in cursor.fetchall()]
        conn2.close()
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "safety.html",
        {
            "active_page": "safety",
            "settings": settings,
            "safe_count": safe_count,
            "blocked_count": blocked_count,
            "ingredient_count": ingredient_count,
            "custom_ingredients": custom_ingredients,
        },
    )
