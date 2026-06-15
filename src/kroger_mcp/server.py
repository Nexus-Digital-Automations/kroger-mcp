#!/usr/bin/env python3
"""
FastMCP Server for Kroger API

This server provides 18 consolidated MCP tools for interacting with the Kroger API.
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
    info_tools,
    ingredient_management_tools,
    location_tools,
    meal_planner_tools,
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
        instructions="""
        This MCP server provides access to Kroger's API for grocery shopping functionality.

        Key Features:
        - Search and manage store locations
        - Find and search products
        - Add items to shopping cart with local tracking
        - Access chain and department information
        - User profile management
        - Purchase predictions and smart shopping suggestions
        - Comprehensive recommendations combining pantry, deals, and predictions
        - Recipe health scoring and per-location cost estimation
        - Reports and data export (spending, predictions, patterns, pantry)
        - Holiday-aware seasonal shopping
        - Item categorization (routine/regular/treat)
        - Ingredient safety filtering for health-optimized shopping

        Health-Optimized Shopping:
        This server includes an evidence-based ingredient filtering system designed
        to optimize for:
        1. General Health - Avoid additives linked to chronic disease outcomes
        2. Cancer Prevention - Flag IARC-classified carcinogens and genotoxic additives
        3. Metabolic Health - Identify blood sugar spiking ingredients and insulin disruptors
        4. Microbiome Optimization - Flag emulsifiers/sweeteners with gut-barrier disruption
        5. Minimizing Ultra-Processed Foods - Detect markers of heavy industrial processing

        Use safety tools to:
        - safety_check(action='check_product') / safety_check(action='check_products') - Scan products for bad ingredients
        - safety_check(action='check_cart') - Scan entire cart for concerns
        - safety_check(action='get_ingredients_list') - View 62+ flagged ingredients with severity levels
        - safety_check(action='configure_settings') - Enable/disable filtering, set block mode
        - safety_products(action='approve') / safety_products(action='block') - Manage personal safe/blocked lists

        Dynamic Ingredient Management:
        Users can fully customize the ingredient filter beyond the default 62:
        - ingredients(action='add') - Add your own ingredients to flag
        - ingredients(action='edit') - Modify custom ingredients
        - ingredients(action='remove') - Remove custom ingredients
        - ingredients(action='list') - View all custom ingredients
        - ingredients(action='override_system') - Change default ingredient settings
        - ingredients(action='reset_system') - Restore system defaults
        - ingredients(action='import') / ingredients(action='export') - Share ingredient lists
        - ingredients(action='preview_impact') - See impact before adding
        - ingredients(action='get_info') - Get detailed ingredient information
        All changes take effect immediately (no restart needed).

        Deal Discovery & Price Tracking:
        The server automatically tracks prices during searches and provides:
        - deals(action='find') - Search for products on sale with significant discounts
        - deals(action='get_price_history') - View price trends and best time to buy
        - deals(action='add_to_watchlist') - Track items for price drops
        - deals(action='scan_watchlist') - Check tracked items for current sales
        - deals(action='score_quality', product_id=...) - Rate one product's current deal vs. 30-day history
        - deals(action='get_latest_scan') - View results from automated background scans
        - Savings summaries in cart views

        Smart Recommendations:
        Combine pantry status, deals, predictions, and favorites into one ranked list:
        - predictions(action='get_smart_recommendations') - Tiered list (urgent / high-value / good-timing / nice-to-have)
        - predictions(action='explain_recommendation', product_id=...) - Why one product was scored that way
        - predictions(action='get_seasonal', holiday='thanksgiving') - Items associated with a holiday
        - predictions(action='get_seasonal') - Upcoming seasonal items (no holiday filter)
        - predictions(action='get_upcoming_holidays', days_ahead=30) - Holidays whose shop-by date is approaching

        Recipe Analysis:
        Every saved recipe links its ingredients to Kroger product IDs (or override=True for non-Kroger items):
        - recipes(action='analyze', recipe_id=...) - Full report: health score, per-location cost, ingredient coverage
        - recipes(action='preview_order', recipe_id=..., scale=2.0) - Preview what would be ordered (with pantry/skip awareness)
        - recipes(action='add_to_cart', recipe_id=..., confirm=False) - Preview, then call again with confirm=True
        - recipes(action='link_ingredient', links=[{recipe_id, ingredient_index, product_id}, ...]) - Batch link unlinked ingredients
        - recipes.get and recipes.list also auto-include health_score / health_grade

        Reports & Data Export:
        - reports(action='get_analytics', report_type='spending', days_back=30)
        - reports(action='get_analytics', report_type='predictions') - Prediction accuracy
        - reports(action='get_analytics', report_type='patterns') - Purchase patterns
        - reports(action='get_analytics', report_type='pantry') - Pantry inventory snapshot
        - reports(action='export_data') - Full export (orders, products, pantry, recipes)
        - reports(action='check_recipe_pantry', recipe_id=...) - What's needed vs. what's stocked
        - reports(action='generate_shopping_list', recipe_ids=[...]) - Multi-recipe consolidated list
        - reports(action='get_cookable_recipes') - Which saved recipes you can make right now

        Background Scanning (Optional):
        Configure automated deal scanning via launchd (Mon/Thu 9 AM):
        - Scans watchlist items automatically
        - Sends macOS notifications when deals found
        - View results with deals(action='get_latest_scan')

        Whole Foods Catalog:
        Track clean/natural foods using safety filter:
        - whole_foods(action='add') - Add products that pass safety checks
        - whole_foods(action='get_catalog') - View tracked whole foods
        - whole_foods(action='scan') - Find qualifying products by category

        User Servings Preference (Household Size):
        Users can set their default servings per meal (household size) via
        info(action='set_servings', servings=N). This preference is automatically used when:
        - Creating new recipes (if servings not explicitly specified)
        - Adding recipes to shopping list (if override not specified)
        - Assigning recipes to meal plans (if servings_override not specified)
        - Displaying recipe information

        The current default can be retrieved with info(action='get_servings').

        IMPORTANT: Always display servings information when discussing recipes,
        ingredients, and shopping lists. This helps users understand quantities
        and ensures proper scaling for their household size.

        Shopping List Workflow:
        The shopping list provides an intermediate storage layer between recipes
        and the cart. This allows users to:
        - Build a consolidated list from multiple recipes
        - Review items before committing to cart
        - Auto-scale ingredients to household servings
        - Skip items already in pantry

        Shopping list workflow:
        1. info(action='set_servings', servings=2) - Set household size (one-time setup)
        2. pantry(action='get_attention') - REQUIRED before adding to list/cart
        3. shopping_list(action='add_recipe', recipe_id=...) - Auto-scales to household default
        4. shopping_list(action='get') - Review consolidated list
        5. shopping_list(action='add_to_cart', confirm=False) - Preview what will be added
        6. shopping_list(action='add_to_cart', confirm=True) - Add to cart and clear list

        Session Requirement for Shopping:
        Before adding items to shopping list OR cart, users MUST call
        pantry(action='get_attention') at least once in the session. This ensures
        they review:
        - Items expiring soon
        - Low inventory alerts
        - Items overdue for repurchase

        One call to pantry(action='get_attention') unlocks all shopping operations
        for the remainder of the session. The requirement resets when the
        conversation ends.

        Common workflows:
        1. Set a preferred location with location(action='set_preferred')
        2. Set household size with info(action='set_servings')
        3. Search for products with products(action='search') (prices automatically tracked)
        4. Find deals with deals(action='find') (by category or search term)
        5. Check product safety with safety_check(action='check_product') before adding
        6. Review pantry with pantry(action='get_attention') (REQUIRED for shopping)
        7. Add recipes to shopping list with shopping_list(action='add_recipe') (auto-scaled)
        8. Review shopping list with shopping_list(action='get')
        9. Add to cart with shopping_list(action='add_to_cart') or cart(action='add')
        10. Use safety_check(action='check_cart') to scan cart for ingredient concerns
        11. View current cart with cart(action='view') (includes savings summary)
        12. Mark order as placed with cart(action='mark_placed')
        13. Get purchase predictions with predictions(action='get_predictions')

        Automatic Pantry Integration:
        The system seamlessly tracks inventory for all items you purchase:
        - cart(action='add') → Automatically begins tracking items in pantry
        - cart(action='mark_placed') → Automatically restocks tracked items to 100%
        This hands-free system learns your consumption patterns and enables
        predictive reordering without manual inventory management.

        Authentication Flow:
        1. Use auth(action='start') to get an authorization URL
        2. Open the URL in your browser and authorize the application
        3. Copy the full redirect URL from your browser
        4. Use auth(action='complete', redirect_url=...) to finish the process

        Cart Tracking & Predictions:
        This server maintains a local record of items added to your cart and uses
        statistical analysis to predict when items need to be repurchased.
        Items are categorized as:
        - routine: Purchased frequently (every 1-14 days) - milk, bread, eggs
        - regular: Purchased occasionally (every 15-60 days) - cleaning supplies
        - treat: Seasonal/holiday items - turkey, candy

        Notion Recipe Sync (optional):
        Mirror your recipe collection to a Notion database with two-way sync:
        - notion(action='setup') - Create Notion database and sync all existing recipes
        - notion(action='sync_all') - Re-push all recipes to Notion
        - notion(action='pull_changes') - Import edits made directly in Notion
        - notion(action='update_tags', recipe_id=..., tags=[...]) - Update tags on one recipe
        - notion(action='bulk_tag', tag='Favorite') - Add a tag to all synced recipes
        - notion(action='get_status') - Show sync health and stats
        - notion(action='view_recipe', recipe_id=...) - Get Notion URL for a recipe

        Notion Setup:
        1. Add NOTION_API_KEY and NOTION_WORKSPACE_ID to your .env file
        2. Call notion(action='setup') to create the database
        3. All future recipe saves/updates/deletes auto-sync to Notion
        """,
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
    _register(reporting_tools, "reporting_tools")
    _register(favorites_tools, "favorites_tools")
    _register(meal_planner_tools, "meal_planner_tools")
    _register(safety_tools, "safety_tools")
    _register(privacy_tools, "privacy_tools")
    _register(deal_tools, "deal_tools")
    _register(ingredient_management_tools, "ingredient_management_tools")
    _register(shopping_list_tools, "shopping_list_tools")
    _register(notion_tools, "notion_tools")

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
