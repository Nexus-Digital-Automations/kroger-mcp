"""
FastAPI web dashboard for Kroger MCP.

Read-only dashboard for browsing recipes, meal plans, favorites, and pantry.
All writes still happen through Claude/MCP.
"""

import os
import signal
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .routes import dashboard, favorites, meal_plan, pantry, recipes
from .routes import cart as cart_page
from .routes import products as products_page
from .routes import shopping_list as shopping_list_page
from .routes import deals as deals_page
from .routes import safety as safety_page
from .routes import ingredients as ingredients_page
from .routes import predictions as predictions_page
from .routes import analytics as analytics_page
from .routes import settings as settings_page
from .routes.api import cart as api_cart
from .routes.api import pantry as api_pantry
from .routes.api import products as api_products
from .routes.api import shopping_list as api_shopping_list
from .routes.api import deals as api_deals
from .routes.api import safety as api_safety
from .routes.api import ingredients as api_ingredients
from .routes.api import predictions as api_predictions
from .routes.api import analytics as api_analytics
from .routes.api import settings as api_settings
from .routes.api import favorites as api_favorites
from .routes.api import recipes as api_recipes
from .routes.api import meal_plan as api_meal_plan

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Smart Shopper", docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Page routes
app.include_router(dashboard.router)
app.include_router(recipes.router)
app.include_router(meal_plan.router)
app.include_router(favorites.router)
app.include_router(pantry.router)
app.include_router(cart_page.router)
app.include_router(products_page.router)
app.include_router(shopping_list_page.router)
app.include_router(deals_page.router)
app.include_router(safety_page.router)
app.include_router(ingredients_page.router)
app.include_router(predictions_page.router)
app.include_router(analytics_page.router)
app.include_router(settings_page.router)

# API routes
app.include_router(api_cart.router)
app.include_router(api_pantry.router)
app.include_router(api_products.router)
app.include_router(api_shopping_list.router)
app.include_router(api_deals.router)
app.include_router(api_safety.router)
app.include_router(api_ingredients.router)
app.include_router(api_predictions.router)
app.include_router(api_analytics.router)
app.include_router(api_settings.router)
app.include_router(api_favorites.router)
app.include_router(api_recipes.router)
app.include_router(api_meal_plan.router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/shutdown", include_in_schema=False)
async def shutdown():
    os.kill(os.getpid(), signal.SIGTERM)
    return {"message": "Server shutting down"}


PORT = 8080


def run():
    import uvicorn
    print(f"Smart Shopper running at http://localhost:{PORT}")
    uvicorn.run("kroger_mcp.web.app:app", host="0.0.0.0", port=PORT, reload=False)


def stop():
    """Kill any process running on the web port."""
    import subprocess
    result = subprocess.run(['lsof', '-ti', f':{PORT}'], capture_output=True, text=True)
    pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
    if not pids:
        print(f"No server found on port {PORT}")
        return
    for pid in pids:
        os.kill(int(pid), signal.SIGTERM)
    print(f"Stopped {len(pids)} process(es) on port {PORT}")
