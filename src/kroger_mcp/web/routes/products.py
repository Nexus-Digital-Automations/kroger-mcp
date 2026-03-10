"""Products search page route."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.tools.shared import get_preferred_location_id

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    location_id = get_preferred_location_id() or "03400014"
    return templates.TemplateResponse("products.html", {
        "request": request,
        "active_page": "products",
        "location_id": location_id,
    })
