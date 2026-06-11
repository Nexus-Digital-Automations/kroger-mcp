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
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
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


def _enforce_env_isolation() -> None:
    """Refuse to start a dev instance against a production database.

    Guards the catastrophic "dev box points at prod Postgres" mistake: unless
    APP_ENV=prod, DATABASE_URL must target a local host (or be unset → SQLite).
    The production mini sets APP_ENV=prod; the dev Air leaves it unset/dev and
    cannot reach the prod DB (which is localhost-bound there anyway).
    """
    from urllib.parse import urlparse

    app_env = os.environ.get("APP_ENV", "dev")
    db_url = os.environ.get("DATABASE_URL", "")
    if app_env == "prod" or not db_url:
        return
    host = urlparse(db_url).hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1", ""):
        raise RuntimeError(
            f"Refusing to start: APP_ENV={app_env!r} but DATABASE_URL targets "
            f"non-local host {host!r}. Set APP_ENV=prod on the production box, "
            f"or point dev at a localhost database."
        )


_enforce_env_isolation()

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

from .routes import auth as auth_routes
from .routes import dashboard, favorites, meal_plan, pantry, recipes
from .routes import deals as deals_page
from .routes import ingredients as ingredients_page
from .routes import products as products_page
from .routes import safety as safety_page
from .routes import settings as settings_page
from .routes import shopping_list as shopping_list_page
from .routes.api import cart as api_cart
from .routes.api import chat as api_chat
from .routes.api import deals as api_deals
from .routes.api import favorites as api_favorites
from .routes.api import ingredients as api_ingredients
from .routes.api import meal_plan as api_meal_plan
from .routes.api import pantry as api_pantry
from .routes.api import products as api_products
from .routes.api import recipes as api_recipes
from .routes.api import safety as api_safety
from .routes.api import settings as api_settings
from .routes.api import shopping_list as api_shopping_list
from .templating import TEMPLATES_DIR, templates  # noqa: F401  (re-exported)

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Per-worker startup/shutdown.

    Creates one pooled async HTTP client used for streaming LLM calls (httpx
    clients hold sockets and must not be shared across a fork — one per worker
    is correct), and warms the ingredient pattern cache so the first chat/scan
    doesn't pay the ~1000-regex compile.
    """
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    try:
        from kroger_mcp.analytics.ingredients import get_compiled_patterns

        get_compiled_patterns()
    except Exception:
        logger.warning("ingredient pattern cache warm failed", exc_info=True)

    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="Smart Shopper", docs_url=None, redoc_url=None, lifespan=lifespan)

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

from kroger_mcp.auth.middleware import AuthMiddleware

# Compress JSON/HTML responses over 1KB — large win for the companion phone over
# LAN/cellular at a tiny, bounded CPU cost. Added before AuthMiddleware so it
# wraps the outermost response.
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(AuthMiddleware)


# Cache-Control policy:
# - Static assets (CSS/JS) are stable content — give them a long max-age so the
#   browser stops re-validating them on every page load.
# - Rendered HTML must NOT be reused: templates change on deploy without bumping
#   any URL, so a cached page would serve stale markup. Force a refetch.
@app.middleware("http")
async def _cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response

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
app.include_router(settings_page.router)

# API routes
app.include_router(api_cart.router)
app.include_router(api_pantry.router)
app.include_router(api_products.router)
app.include_router(api_shopping_list.router)
app.include_router(api_deals.router)
app.include_router(api_safety.router)
app.include_router(api_ingredients.router)
app.include_router(api_settings.router)
app.include_router(api_favorites.router)
app.include_router(api_recipes.router)
app.include_router(api_meal_plan.router)
app.include_router(api_chat.router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/shutdown", include_in_schema=False)
async def shutdown():
    os.kill(os.getpid(), signal.SIGTERM)
    return {"message": "Server shutting down"}


PORT = int(os.environ.get("WEB_PORT", 8000))
# Multiple workers spread blocking work (DB, threadpool'd tool handlers) across
# processes. Each worker holds its own httpx client + DB pool (see lifespan).
# Default 2 — deliberately conservative: the production mini is SHARED (mempalace
# ~1.3GB + remote-access tooling), so more workers would starve the other
# tenants. Override per host only on a box dedicated to this app.
WORKERS = int(os.environ.get("WEB_WORKERS", 2))


def run():
    import uvicorn

    stop()
    print(f"Smart Shopper running at http://localhost:{PORT} ({WORKERS} worker(s))")
    # 0.0.0.0 bind is intentional — this is a local dev/single-user tool meant
    # to be reachable from any interface on the host (e.g. companion phone on
    # the LAN). bandit B104 flagged.
    #
    # uvloop + httptools (project deps) replace asyncio + h11 for a faster event
    # loop and C HTTP parser — lower CPU per request, which is the good-neighbor
    # way to add throughput on the shared box (vs. adding workers).
    uvicorn.run(
        "kroger_mcp.web.app:app",
        host="0.0.0.0",  # nosec B104
        port=PORT,
        reload=False,
        workers=WORKERS,
        loop="uvloop",
        http="httptools",
    )


def _pids_on_port() -> list[int]:
    """PIDs currently bound to the web port (via lsof)."""
    import subprocess  # nosec B404 - static `lsof` invocation, no shell, no user input

    # `lsof -ti :{PORT}` — args are a static list; PORT is an integer parsed
    # from env at import time. No shell, no user-controlled input.
    result = subprocess.run(  # nosec B603 B607
        ["lsof", "-ti", f":{PORT}"], capture_output=True, text=True
    )
    return [int(p) for p in result.stdout.split() if p.strip()]


def stop():
    """Free the web port and WAIT until it is actually released.

    Returning before the port is free lets the subsequent ``uvicorn`` bind race a
    still-dying worker and fail with EADDRINUSE. Under launchd KeepAlive that
    becomes a restart loop that accumulates orphaned, non-serving workers (which
    is exactly how a hard ``kickstart -k`` could wedge the service). So: SIGTERM,
    poll until the port clears, then SIGKILL any straggler, and confirm free
    before returning.
    """
    import time

    pids = _pids_on_port()
    if not pids:
        print(f"No server found on port {PORT}")
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Up to ~6s for graceful shutdown + socket release.
    for _ in range(30):
        time.sleep(0.2)
        if not _pids_on_port():
            print(f"Stopped {len(pids)} process(es) on port {PORT}")
            return
    # Stragglers: escalate to SIGKILL, then give the socket a moment to release.
    for pid in _pids_on_port():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    print(f"Force-stopped stragglers on port {PORT}")
