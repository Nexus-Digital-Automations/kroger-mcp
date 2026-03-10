"""Settings page route."""
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from kroger_mcp.tools.shared import get_preferred_location_id, get_default_servings

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    location_id = get_preferred_location_id() or ""
    servings = get_default_servings()

    # Check auth status
    auth_status = "not_configured"
    try:
        from kroger_mcp.tools.shared import get_authenticated_client
        get_authenticated_client()
        auth_status = "authenticated"
    except Exception as exc:
        if "Authentication required" in str(exc):
            auth_status = "not_authenticated"
        else:
            auth_status = "not_configured"

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "active_page": "settings",
        "location_id": location_id,
        "servings": servings,
        "auth_status": auth_status,
    })
