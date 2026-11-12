#!/usr/bin/env python3
"""
FastMCP Server for Kroger API

This server provides 19 consolidated MCP tools for interacting with the Kroger API.
Each tool uses an action-based dispatch pattern: tool(action='...', params...)

Environment Variables Required:
- KROGER_CLIENT_ID: Your Kroger API client ID
- KROGER_CLIENT_SECRET: Your Kroger API client secret
- KROGER_REDIRECT_URI: Redirect URI for OAuth2 flow (default: http://localhost:8000/callback)
- KROGER_USER_ZIP_CODE: Default zip code for location searches (optional)
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time

from fastmcp import FastMCP

# Activity middleware powers the idle watchdog (below) and needs FastMCP 3.x.
# Prod ships 3.3.1 (uv.lock, pinned); guard the import so the module still loads
# under an older fastmcp (e.g. a stale system test env), where main() never runs.
try:
    from fastmcp.server.middleware import Middleware

    _HAS_MIDDLEWARE = True
except ImportError:  # pragma: no cover - only on fastmcp < 3
    Middleware = object  # type: ignore[assignment,misc]
    _HAS_MIDDLEWARE = False

# Import database initializer — run at startup to avoid first-call migration hang
from .analytics.database import ensure_initialized

# Import prompts
from .config import prompts

# Import session state manager
from .config.session_state import get_session_manager

# Import all tool modules
from .tools import (
    auth_tools,
    cart_tools,
    deal_tools,
    favorites_tools,
    guide_tools,
    info_tools,
    ingredient_management_tools,
    location_tools,
    meal_planner_tools,
    notification_tools,
    notion_tools,
    prediction_tools,
    privacy_tools,
    product_tools,
    recipe_tools,
    reporting_tools,
    safety_tools,
    shopping_list_tools,
)

logger = logging.getLogger(__name__)


async def _cleanup_stale_sessions():
    """Background task to cleanup stale sessions."""
    session_manager = get_session_manager()
    while True:
        await asyncio.sleep(3600)  # 1 hour
        session_manager.cleanup_stale_sessions(max_age_hours=24)


# ── Idle-watchdog: stop per-session ssh-stdio leaks ──────────────────────────
# kroger-mcp is launched once per Claude session over ssh-stdio. The ssh channel
# can stay open+idle for days after the client is gone, so stdin never reaches
# EOF and the server would run forever — one leaked process per session. The
# watchdog exits the process after KROGER_MCP_IDLE_TIMEOUT seconds with no MCP
# message, or immediately once reparented to launchd (the ssh launcher died). The
# client transparently respawns the server on next use.
_last_activity = time.monotonic()


class _ActivityMiddleware(Middleware):
    """Bump the idle clock on every inbound MCP message."""

    async def on_message(self, context, call_next):
        global _last_activity
        _last_activity = time.monotonic()
        return await call_next(context)


def _idle_exit_due(now: float, last_activity: float, timeout: float, ppid: int) -> bool:
    """True when the server should self-exit: idle past timeout, or orphaned."""
    return (now - last_activity) >= timeout or ppid == 1


def _install_idle_watchdog() -> None:
    """Start the daemon watchdog thread (no-op if KROGER_MCP_IDLE_TIMEOUT<=0)."""
    try:
        timeout = float(os.environ.get("KROGER_MCP_IDLE_TIMEOUT", "1800"))
    except ValueError:
        timeout = 1800.0
    if timeout <= 0:
        return

    def _watch() -> None:
        poll = min(60.0, max(1.0, timeout / 2))
        while True:
            time.sleep(poll)
            if _idle_exit_due(time.monotonic(), _last_activity, timeout, os.getppid()):
                orphaned = os.getppid() == 1
                logger.info(
                    "kroger-mcp self-exit (%s)",
                    "orphaned" if orphaned else f"idle >= {timeout:.0f}s",
                )
                os._exit(0)

    threading.Thread(target=_watch, name="kroger-mcp-idle-watchdog", daemon=True).start()


def _install_signal_exit() -> None:
    """Exit cleanly on SIGTERM/SIGHUP (e.g. ssh session teardown)."""

    def _handler(signum, _frame):
        logger.info("kroger-mcp exiting on signal %s", signum)
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not running in the main thread / unsupported platform


# Kept short on purpose: every client pays for this text each session, and each
# tool already lists its own actions. Only cross-tool rules and workflows go here.
_INSTRUCTIONS = """Kroger grocery assistant: products, cart, pantry, recipes, meal plans,
favorites, deals, predictions, ingredient-safety filtering. Each tool takes an
`action`; its description lists the actions.

Rules:
- Call pantry(action='get_attention') once per session before shopping
  (shopping_list add_recipe/add_to_cart refuse without it). It also returns
  alerts and the weekly draft.
- Anything that spends money previews first: cart(action='add',
  preview_only=True) and *(action='add_to_cart'|'order', confirm=False). Get
  an explicit yes (and PICKUP or DELIVERY) before confirm=True.
- safety(action='check_product'|'check_products'|'check_cart') screens
  ingredients; ingredients(action=...) customizes the flagged list.
- Recipe ingredients need only `name`; a product_id links one to Kroger,
  none means it is bought elsewhere (manual).
- Household servings: info(action='get_servings'|'set_servings') scales
  recipes, lists and plans; show servings when discussing quantities.

Workflows:
- Recipe to cart: shopping_list(add_recipe) -> get -> add_to_cart(confirm=False)
  -> add_to_cart(confirm=True). One-off items: shopping_list(add_item).
- Weekly: past planned meals deduct pantry automatically; correct with
  meal_plan(skip_meal|undo_cooked). Approve drafts with meal_plan(approve_draft).
- Snacks eaten: favorites(log_snack, item='chips').
- After checkout: cart(mark_placed) restocks the pantry.
- Auth: auth(start) -> user opens URL -> auth(complete, redirect_url=...).
"""


def create_server() -> FastMCP:
    """Create and configure the FastMCP server instance"""
    # Run DB migration at startup, not on the first live tool call.
    # ensure_initialized() is idempotent — safe to call multiple times.
    # Without this, the first pantry/safety/meal-planner tool call after a
    # fresh server start would block the event loop for the full migration
    # duration, causing the MCP client to time out and requiring a reboot.
    ensure_initialized()

    # TODO: Implement session cleanup using FastMCP lifecycle hooks
    # The _cleanup_stale_sessions() function is defined above but not currently
    # scheduled because asyncio.create_task() requires a running event loop.
    # Once FastMCP startup hooks are implemented, schedule it there.

    # Initialize the FastMCP server
    mcp: FastMCP = FastMCP(
        name="Kroger API Server",
        instructions=_INSTRUCTIONS,
    )

    def _register(module, name):
        try:
            module.register_tools(mcp)
        except Exception as e:
            logger.error("Failed to register %s: %s", name, e)

    # Register all tools from the modules
    _register(location_tools, "location_tools")
    _register(product_tools, "product_tools")
    _register(cart_tools, "cart_tools")
    _register(info_tools, "info_tools")
    _register(auth_tools, "auth_tools")
    _register(prediction_tools, "prediction_tools")
    _register(recipe_tools, "recipe_tools")
    _register(guide_tools, "guide_tools")
    _register(reporting_tools, "reporting_tools")
    _register(favorites_tools, "favorites_tools")
    _register(meal_planner_tools, "meal_planner_tools")
    _register(safety_tools, "safety_tools")
    _register(privacy_tools, "privacy_tools")
    _register(deal_tools, "deal_tools")
    _register(ingredient_management_tools, "ingredient_management_tools")
    _register(shopping_list_tools, "shopping_list_tools")
    _register(notion_tools, "notion_tools")
    _register(notification_tools, "notification_tools")

    if _HAS_MIDDLEWARE:
        from .token_slim import TokenSlimMiddleware

        mcp.add_middleware(TokenSlimMiddleware())

    # Register prompts
    try:
        prompts.register_prompts(mcp)
    except Exception as e:
        logger.error("Failed to register prompts: %s", e)

    return mcp


def main():
    """Main entry point for the Kroger MCP server"""
    mcp = create_server()
    _install_signal_exit()  # SIGTERM/SIGHUP exit is always safe
    # Activity tracking + idle watchdog go together: the watchdog must only run
    # when the middleware is feeding it heartbeats, else it would reap an active
    # server. Prod (fastmcp 3.3.1) has both; older fastmcp degrades to no watchdog.
    if _HAS_MIDDLEWARE and hasattr(mcp, "add_middleware"):
        mcp.add_middleware(_ActivityMiddleware())
        _install_idle_watchdog()
    else:
        logger.warning("fastmcp middleware unavailable; kroger-mcp idle watchdog disabled")
    mcp.run()


if __name__ == "__main__":
    main()
