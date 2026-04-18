"""
FastAPI web dashboard for Kroger MCP.

Read-only dashboard for browsing recipes, meal plans, favorites, and pantry.
All writes still happen through Claude/MCP.
"""
# ruff: noqa: E402  -- env must be loaded before FastAPI imports

import os
import signal
from pathlib import Path

# Load .env from project root before any other imports that need env vars
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

# Fall back to Claude Desktop config for Kroger credentials if not in env
def _load_claude_desktop_env():
    """Load Kroger credentials from Claude Desktop config if not already set."""
    if os.environ.get("KROGER_CLIENT_ID"):
        return
    config_path = (
        Path.home()
        / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )
    if not config_path.exists():
        return
    try:
        import json
        config = json.loads(config_path.read_text())
        for server in config.get("mcpServers", {}).values():
            env = server.get("env", {})
            if env.get("KROGER_CLIENT_ID"):
                for key, val in env.items():
                    if key.startswith("KROGER_") and not os.environ.get(key):
                        os.environ[key] = val
                return
    except Exception:
        pass

_load_claude_desktop_env()

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .routes import dashboard, favorites, meal_plan, pantry, recipes
from .routes import products as products_page
from .routes import shopping_list as shopping_list_page
from .routes import deals as deals_page
from .routes import safety as safety_page
from .routes import ingredients as ingredients_page
from .routes import predictions as predictions_page
from .routes import analytics as analytics_page
from .routes import meal_tracker as meal_tracker_page
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
from .routes.api import meal_tracker as api_meal_tracker
from .routes.api import chat as api_chat
from .routes import auth as auth_routes

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Smart Shopper", docs_url=None, redoc_url=None)

# Shared CSS + JS for the unified action-menu dropdown (see
# static/js/action_menu.js and templates/_macros/action_menu.html).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Initialize auth tables (SQLite dev mode)
try:
    from kroger_mcp.analytics.database import get_backend
    if get_backend() == "sqlite":
        from kroger_mcp.analytics.pg_database import initialize_sqlite_auth_tables
        initialize_sqlite_auth_tables()
except Exception:
    pass

# Auth middleware — uncomment to enable login requirement:
# from kroger_mcp.auth.middleware import AuthMiddleware
# app.add_middleware(AuthMiddleware)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Auth routes (login, register, logout)
app.include_router(auth_routes.router)

# Page routes
app.include_router(dashboard.router)
app.include_router(recipes.router)
app.include_router(meal_plan.router)
app.include_router(favorites.router)
app.include_router(pantry.router)
app.include_router(products_page.router)
app.include_router(shopping_list_page.router)
app.include_router(deals_page.router)
app.include_router(safety_page.router)
app.include_router(ingredients_page.router)
app.include_router(predictions_page.router)
app.include_router(analytics_page.router)
app.include_router(meal_tracker_page.router)
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
app.include_router(api_meal_tracker.router)
app.include_router(api_chat.router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/shutdown", include_in_schema=False)
async def shutdown():
    os.kill(os.getpid(), signal.SIGTERM)
    return {"message": "Server shutting down"}


PORT = int(os.environ.get("WEB_PORT", 8000))


def run():
    import uvicorn
    stop()
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
