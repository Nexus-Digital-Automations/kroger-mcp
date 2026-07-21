"""Shopping list page route."""


from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kroger_mcp.analytics.database import run_in_thread
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.tools.recipe_tools import _load_recipes
from kroger_mcp.tools.shared import get_default_servings
from kroger_mcp.tools.shopping_list_tools import _load_shopping_list
from kroger_mcp.web.context import action_menu_context
from kroger_mcp.web.templating import templates

router = APIRouter()


@router.get("/cart", response_class=RedirectResponse)
async def cart_redirect():
    return RedirectResponse(url="/shopping-list", status_code=301)


def _shopping_list_payload(user_id: str) -> dict:
    """All blocking work for the shopping-list page, run off the event loop
    via run_in_thread. user_id MUST be the authed user: the bare
    _load_shopping_list() resolves to the migration-default user, which showed
    every other account the wrong list (pre-existing bug, fixed here)."""
    sl_data = _load_shopping_list(user_id=user_id)
    recipe_data = _load_recipes()
    recipes = recipe_data.get("recipes", [])
    items = sl_data.get("items", [])
    servings = get_default_servings(user_id=user_id)
    return {
        "active_page": "shopping_list",
        "items": items,
        "recipes": recipes,
        "default_servings": servings,
        "item_count": len(items),
        **action_menu_context(user_id),
    }


@router.get("/shopping-list", response_class=HTMLResponse)
async def shopping_list_page(request: Request):
    context = await run_in_thread(_shopping_list_payload, current_user_id(request))
    return templates.TemplateResponse(request, "shopping_list.html", context)
