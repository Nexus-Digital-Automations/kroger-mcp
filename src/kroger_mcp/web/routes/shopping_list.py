"""Shopping list page route."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.tools.shopping_list_tools import _load_shopping_list
from kroger_mcp.tools.recipe_tools import _load_recipes
from kroger_mcp.tools.shared import get_default_servings

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/cart", response_class=RedirectResponse)
async def cart_redirect():
    return RedirectResponse(url="/shopping-list", status_code=301)


@router.get("/shopping-list", response_class=HTMLResponse)
async def shopping_list_page(request: Request):
    sl_data = _load_shopping_list()
    recipe_data = _load_recipes()
    recipes = recipe_data.get("recipes", [])
    items = sl_data.get("items", [])
    servings = get_default_servings()
    return templates.TemplateResponse("shopping_list.html", {
        "request": request,
        "active_page": "shopping_list",
        "items": items,
        "recipes": recipes,
        "default_servings": servings,
        "item_count": len(items),
    })
