"""
Multi-provider chat engine for Smart Shopper.

Provides:
- Tool registry mapping function names to handlers with read/write classification
- A generic OpenAI-compatible client + provider registry (DeepSeek, OpenAI,
  OpenRouter, Groq, Together, Mistral) with per-provider env-var API keys
- Conversation orchestrator with approval flow for mutating actions
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any

import requests

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Smart Shopper's personal chef and grocery assistant. You help users manage their Kroger grocery shopping through conversation.

Your personality:
- Culinary expert with knowledge of food history, cultural traditions, and flavor science
- Health-focused: prioritize whole foods, minimize ultra-processed items
- Flavor first: never sacrifice taste for convenience
- Knowledgeable but never pretentious
- Concise and helpful

Your store: Kroger — 336 North Loop, Conroe, TX (Location ID: 03400014)

Guidelines:
- Always check product safety before recommending items for purchase
- Suggest seasonal produce when relevant
- When users want to modify their cart, pantry, recipes, or meal plans, describe the action clearly so they can approve it
- Format results clearly with prices and safety info when available
- Be conversational and friendly, not robotic
- If a search returns no results, suggest alternative search terms
- When listing products, include price and brand"""

# ---------------------------------------------------------------------------
# LLM providers (OpenAI-compatible /chat/completions)
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 30  # Truncate to prevent token overflow

# Every preset below speaks the OpenAI-compatible chat-completions schema, so a
# single client serves all of them. Adding a provider = one entry here. Default
# models are the cheapest option per provider that reliably supports the tool
# calling this assistant depends on.
PROVIDER_REGISTRY: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "deepseek/deepseek-chat",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "api_key_env": "GROQ_API_KEY",
    },
    "together": {
        "label": "Together",
        "base_url": "https://api.together.xyz/v1/chat/completions",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "api_key_env": "TOGETHER_API_KEY",
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1/chat/completions",
        "model": "mistral-small-latest",
        "api_key_env": "MISTRAL_API_KEY",
    },
}

DEFAULT_PROVIDER = "deepseek"


class OpenAICompatibleClient:
    """Chat-completions client for any OpenAI-compatible provider.

    Failure modes: returns {"error": True, "message": ...} (never raises) for a
    missing key, non-200 response, timeout, or connection error, so callers can
    surface the message to the user unchanged.
    """

    def __init__(self, provider_id: str):
        preset = PROVIDER_REGISTRY[provider_id]  # caller guarantees a valid id
        self.provider_id = provider_id
        self.label = preset["label"]
        self.api_url = preset["base_url"]
        self.model = preset["model"]
        self._key_env = preset["api_key_env"]
        self.api_key = os.environ.get(self._key_env, "")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            logger.warning(
                "chat requested for provider=%s but %s is not set",
                self.provider_id,
                self._key_env,
            )
            return {
                "error": True,
                "message": (
                    f"{self.label} API key not configured. "
                    f"Add {self._key_env} to your .env file."
                ),
            }

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=45,
            )
            if resp.status_code != 200:
                body = resp.text[:300]
                logger.error(
                    "provider=%s model=%s http=%s body=%s",
                    self.provider_id,
                    self.model,
                    resp.status_code,
                    body,
                )
                return {
                    "error": True,
                    "message": f"{self.label} API error ({resp.status_code}): {body}",
                }
            return resp.json()
        except requests.Timeout:
            logger.error("provider=%s request timed out", self.provider_id)
            return {
                "error": True,
                "message": f"{self.label} API request timed out. Please try again.",
            }
        except requests.ConnectionError:
            logger.error("provider=%s connection error", self.provider_id)
            return {
                "error": True,
                "message": f"Could not connect to {self.label} API. Check your internet connection.",
            }
        except Exception as exc:
            logger.error(
                "provider=%s request failed: %s",
                self.provider_id,
                exc,
                exc_info=True,
            )
            return {"error": True, "message": f"{self.label} request failed: {str(exc)[:200]}"}


_client_cache: dict[str, OpenAICompatibleClient] = {}


def get_client(provider_id: str | None = None) -> OpenAICompatibleClient:
    """Return a cached client for provider_id, falling back to DEFAULT_PROVIDER.

    An unknown id is treated as the default (logged at WARNING) so a stale or
    malformed frontend selection degrades gracefully instead of erroring.
    """
    pid = provider_id or DEFAULT_PROVIDER
    if pid not in PROVIDER_REGISTRY:
        logger.warning("unknown provider=%r; falling back to %s", pid, DEFAULT_PROVIDER)
        pid = DEFAULT_PROVIDER
    if pid not in _client_cache:
        _client_cache[pid] = OpenAICompatibleClient(pid)
    return _client_cache[pid]


def list_available_providers() -> list[dict[str, str]]:
    """Providers whose API key is configured. Never exposes the keys themselves."""
    available: list[dict[str, str]] = []
    for pid, preset in PROVIDER_REGISTRY.items():
        if os.environ.get(preset["api_key_env"], "").strip():
            available.append({"id": pid, "label": preset["label"], "model": preset["model"]})
    return available


# ---------------------------------------------------------------------------
# Tool handlers — read-only
# ---------------------------------------------------------------------------


def _handle_search_products(search_term: str, limit: int = 10) -> dict[str, Any]:
    """Search Kroger products."""
    try:
        from kroger_mcp.tools.shared import (
            get_client_credentials_client,
            get_preferred_location_id,
        )
        from kroger_mcp.web.routes.api.products import _extract_product

        client = get_client_credentials_client()
        location_id = get_preferred_location_id() or "03400014"

        clean_term = re.sub(r"[^\w\s-]", "", search_term).strip()[:50]
        if not clean_term:
            return {"error": "Invalid search term"}

        result = client.product.search_products(
            term=clean_term,
            location_id=location_id,
            limit=min(limit, 20),
        )

        raw = []
        if isinstance(result, dict):
            raw = result.get("data", []) or []
        elif hasattr(result, "data"):
            raw = result.data or []
        elif isinstance(result, list):
            raw = result

        products = [
            p for p in (_extract_product(item) for item in raw) if p and p.get("product_id")
        ]

        # Enrich with safety scores
        try:
            from kroger_mcp.analytics.safety import check_products_safety_batch

            statuses = check_products_safety_batch(products)
            for product, status in zip(products, statuses, strict=False):
                d = status.to_dict()
                product["safety_grade"] = d.get("safety_grade")
                product["flagged_ingredients"] = d.get("flagged_ingredients", [])
        except Exception:
            pass

        return {"products": products, "count": len(products)}
    except Exception as exc:
        return {"error": f"Product search failed: {str(exc)[:200]}"}


def _handle_view_cart() -> dict[str, Any]:
    """View current cart contents."""
    try:
        from kroger_mcp.tools.cart_tools import _load_cart_data

        return _load_cart_data()
    except Exception as exc:
        return {"error": f"Failed to load cart: {str(exc)[:200]}"}


def _handle_get_pantry() -> dict[str, Any]:
    """Get pantry inventory."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.pantry import get_pantry_status

        ensure_initialized()
        items = get_pantry_status(apply_depletion=True)
        return {"items": items, "count": len(items)}
    except Exception as exc:
        return {"error": f"Failed to get pantry: {str(exc)[:200]}"}


def _handle_get_low_inventory(threshold: int = 20) -> dict[str, Any]:
    """Get low inventory pantry items."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.pantry import get_low_inventory_items

        ensure_initialized()
        items = get_low_inventory_items(threshold=threshold)
        return {"items": items, "count": len(items)}
    except Exception as exc:
        return {"error": f"Failed to get low inventory: {str(exc)[:200]}"}


def _handle_list_recipes() -> dict[str, Any]:
    """List all saved recipes."""
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes

        data = _load_recipes()
        recipes = data.get("recipes", [])
        summary = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "description": r.get("description", ""),
                "servings": r.get("servings"),
                "tags": r.get("tags", []),
                "ingredient_count": len(r.get("ingredients", [])),
            }
            for r in recipes
        ]
        return {"recipes": summary, "count": len(summary)}
    except Exception as exc:
        return {"error": f"Failed to list recipes: {str(exc)[:200]}"}


def _handle_get_recipe(recipe_id: str) -> dict[str, Any]:
    """Get a specific recipe by ID."""
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes

        data = _load_recipes()
        recipe = next((r for r in data.get("recipes", []) if r.get("id") == recipe_id), None)
        if not recipe:
            return {"error": f"Recipe {recipe_id!r} not found"}
        return {"recipe": recipe}
    except Exception as exc:
        return {"error": f"Failed to get recipe: {str(exc)[:200]}"}


def _handle_search_recipes(query: str) -> dict[str, Any]:
    """Search recipes by name or tag."""
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes

        data = _load_recipes()
        q = query.lower()
        matches = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "description": r.get("description", ""),
                "tags": r.get("tags", []),
                "ingredient_count": len(r.get("ingredients", [])),
            }
            for r in data.get("recipes", [])
            if q in r.get("name", "").lower()
            or q in r.get("description", "").lower()
            or any(q in t.lower() for t in r.get("tags", []))
        ]
        return {"recipes": matches, "count": len(matches)}
    except Exception as exc:
        return {"error": f"Recipe search failed: {str(exc)[:200]}"}


def _handle_list_meal_plans() -> dict[str, Any]:
    """List meal plans."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.meal_planning import list_plans_for_api

        ensure_initialized()
        return list_plans_for_api()
    except Exception as exc:
        return {"error": f"Failed to list meal plans: {str(exc)[:200]}"}


def _handle_get_favorites_lists() -> dict[str, Any]:
    """Get all favorites lists."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.favorites import get_lists

        ensure_initialized()
        lists = get_lists()
        return {"lists": [lst for lst in lists if not lst.get("is_default")]}
    except Exception as exc:
        return {"error": f"Failed to get favorites: {str(exc)[:200]}"}


def _handle_get_favorites_items(list_id: str) -> dict[str, Any]:
    """Get items in a favorites list."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.favorites import get_list_items

        ensure_initialized()
        items = get_list_items(list_id)
        return {"items": items, "count": len(items)}
    except Exception as exc:
        return {"error": f"Failed to get favorites items: {str(exc)[:200]}"}


def _handle_check_product_safety(product_id: str) -> dict[str, Any]:
    """Check product ingredient safety."""
    try:
        from kroger_mcp.analytics.safety import check_products_safety_batch

        products = [{"product_id": product_id}]
        statuses = check_products_safety_batch(products)
        if statuses:
            return statuses[0].to_dict()
        return {"error": "No safety data available"}
    except Exception as exc:
        return {"error": f"Safety check failed: {str(exc)[:200]}"}


def _handle_find_deals(search_term: str = "", category: str = "") -> dict[str, Any]:
    """Find deals on products."""
    try:
        from kroger_mcp.tools.shared import (
            get_client_credentials_client,
            get_preferred_location_id,
        )
        from kroger_mcp.web.routes.api.products import _extract_product

        term = search_term or category
        if not term:
            return {"error": "Provide a search_term or category"}

        client = get_client_credentials_client()
        location_id = get_preferred_location_id() or "03400014"

        result = client.product.search_products(
            term=re.sub(r"[^\w\s-]", "", term).strip()[:50],
            location_id=location_id,
            limit=20,
        )

        raw = []
        if isinstance(result, dict):
            raw = result.get("data", []) or []
        elif hasattr(result, "data"):
            raw = result.data or []

        products = [p for p in (_extract_product(item) for item in raw) if p and p.get("on_sale")]
        return {"deals": products, "count": len(products)}
    except Exception as exc:
        return {"error": f"Deal search failed: {str(exc)[:200]}"}


def _handle_get_price_history(product_id: str) -> dict[str, Any]:
    """Get price history for a product."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.deals import get_price_statistics

        ensure_initialized()
        stats = get_price_statistics(product_id)
        return stats if stats else {"message": "No price history available for this product"}
    except Exception as exc:
        return {"error": f"Price history failed: {str(exc)[:200]}"}


def _handle_get_shopping_list() -> dict[str, Any]:
    """Get current shopping list."""
    try:
        from kroger_mcp.tools.shopping_list_tools import _load_shopping_list

        return _load_shopping_list()
    except Exception as exc:
        return {"error": f"Failed to get shopping list: {str(exc)[:200]}"}


def _handle_get_cookable_recipes() -> dict[str, Any]:
    """Get recipes that can be made with current pantry items."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.recipe_integration import get_cookable_recipes

        ensure_initialized()
        result = get_cookable_recipes()
        return result
    except Exception as exc:
        return {"error": f"Failed to get cookable recipes: {str(exc)[:200]}"}


# ---------------------------------------------------------------------------
# Tool handlers — mutating (require approval)
# ---------------------------------------------------------------------------


def _handle_add_to_cart(
    product_id: str,
    quantity: int = 1,
    modality: str = "PICKUP",
    description: str = "",
) -> dict[str, Any]:
    """Add item to local cart."""
    try:
        from kroger_mcp.tools.cart_tools import _add_item_to_local_cart

        _add_item_to_local_cart(
            product_id=product_id,
            quantity=quantity,
            modality=modality,
            product_details={"description": description} if description else None,
        )
        return {"success": True, "product_id": product_id, "quantity": quantity}
    except Exception as exc:
        return {"error": f"Failed to add to cart: {str(exc)[:200]}"}


def _handle_remove_from_cart(product_id: str) -> dict[str, Any]:
    """Remove item from cart."""
    try:
        from kroger_mcp.tools.cart_tools import _load_cart_data, _save_cart_data

        cart_data = _load_cart_data()
        current = cart_data.get("current_cart", [])
        original_len = len(current)
        cart_data["current_cart"] = [i for i in current if i.get("product_id") != product_id]
        if len(cart_data["current_cart"]) == original_len:
            return {"error": f"Item {product_id!r} not found in cart"}
        cart_data["last_updated"] = datetime.now().isoformat()
        _save_cart_data(cart_data)
        return {"success": True, "removed": product_id}
    except Exception as exc:
        return {"error": f"Failed to remove from cart: {str(exc)[:200]}"}


def _handle_clear_cart() -> dict[str, Any]:
    """Clear all items from cart."""
    try:
        from kroger_mcp.tools.cart_tools import _load_cart_data, _save_cart_data

        cart_data = _load_cart_data()
        cart_data["current_cart"] = []
        cart_data["last_updated"] = datetime.now().isoformat()
        _save_cart_data(cart_data)
        return {"success": True, "message": "Cart cleared"}
    except Exception as exc:
        return {"error": f"Failed to clear cart: {str(exc)[:200]}"}


def _handle_add_pantry_item(
    product_id: str,
    description: str = "",
    level: int = 100,
) -> dict[str, Any]:
    """Add item to pantry."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.pantry import add_to_pantry

        ensure_initialized()
        return add_to_pantry(product_id=product_id, description=description, level=level)
    except Exception as exc:
        return {"error": f"Failed to add pantry item: {str(exc)[:200]}"}


def _handle_update_pantry_level(product_id: str, level_percent: int) -> dict[str, Any]:
    """Update pantry item level."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.pantry import update_pantry_level

        ensure_initialized()
        return update_pantry_level(product_id, level_percent)
    except Exception as exc:
        return {"error": f"Failed to update pantry level: {str(exc)[:200]}"}


def _handle_remove_pantry_item(product_id: str) -> dict[str, Any]:
    """Remove item from pantry."""
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.pantry import remove_from_pantry

        ensure_initialized()
        return remove_from_pantry(product_id)
    except Exception as exc:
        return {"error": f"Failed to remove pantry item: {str(exc)[:200]}"}


def _handle_delete_recipe(recipe_id: str) -> dict[str, Any]:
    """Delete a recipe."""
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes, _save_recipes

        data = _load_recipes()
        original = len(data.get("recipes", []))
        data["recipes"] = [r for r in data.get("recipes", []) if r.get("id") != recipe_id]
        if len(data["recipes"]) == original:
            return {"error": f"Recipe {recipe_id!r} not found"}
        _save_recipes(data)
        return {"success": True, "recipe_id": recipe_id}
    except Exception as exc:
        return {"error": f"Failed to delete recipe: {str(exc)[:200]}"}


def _handle_add_recipe_to_shopping_list(
    recipe_id: str, servings: int | None = None
) -> dict[str, Any]:
    """Add recipe ingredients to shopping list."""
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes
        from kroger_mcp.tools.shopping_list_tools import (
            _generate_list_item_id,
            _load_shopping_list,
            _save_shopping_list,
        )

        data = _load_recipes()
        recipe = next((r for r in data.get("recipes", []) if r.get("id") == recipe_id), None)
        if not recipe:
            return {"error": f"Recipe {recipe_id!r} not found"}

        target_servings = servings or recipe.get("servings", 4)
        recipe_servings = recipe.get("servings", 4)
        scale = target_servings / recipe_servings if recipe_servings else 1

        shopping = _load_shopping_list()
        items = shopping.get("items", [])

        added = 0
        for ing in recipe.get("ingredients", []):
            pid = ing.get("product_id")
            if not pid and not ing.get("override"):
                continue
            qty = ing.get("quantity", 1)
            scaled_qty = round(qty * scale, 2) if isinstance(qty, int | float) else qty
            items.append(
                {
                    "id": _generate_list_item_id(),
                    "product_id": pid,
                    "description": ing.get("name", ""),
                    "quantity": scaled_qty,
                    "unit": ing.get("unit", ""),
                    "source_recipe": recipe.get("name", ""),
                    "source_recipe_id": recipe_id,
                    "added_at": datetime.now().isoformat(),
                }
            )
            added += 1

        shopping["items"] = items
        _save_shopping_list(shopping)
        return {"success": True, "added": added, "recipe": recipe.get("name")}
    except Exception as exc:
        return {"error": f"Failed to add to shopping list: {str(exc)[:200]}"}


def _handle_set_servings(servings: int) -> dict[str, Any]:
    """Set default household servings."""
    try:
        import json as _json
        from pathlib import Path

        prefs_file = Path(__file__).parent.parent.parent.parent / "kroger_preferences.json"
        prefs = {}
        if prefs_file.exists():
            prefs = _json.loads(prefs_file.read_text())
        prefs["default_servings"] = servings
        prefs_file.write_text(_json.dumps(prefs, indent=2))
        return {"success": True, "servings": servings}
    except Exception as exc:
        return {"error": f"Failed to set servings: {str(exc)[:200]}"}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


def _p(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Shorthand for building a JSON Schema parameters block."""
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    # ── Read-only ──────────────────────────────────────────────────────────
    "search_products": {
        "handler": _handle_search_products,
        "mutating": False,
        "description": "Search Kroger products by name. Returns product IDs, prices, brands, and safety grades.",
        "parameters": _p(
            {
                "search_term": {
                    "type": "string",
                    "description": 'Product name to search for, e.g. "organic eggs"',
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, max 20)",
                    "default": 10,
                },
            },
            ["search_term"],
        ),
    },
    "view_cart": {
        "handler": _handle_view_cart,
        "mutating": False,
        "description": "View current shopping cart contents with items, quantities, and prices.",
        "parameters": _p({}),
    },
    "get_pantry": {
        "handler": _handle_get_pantry,
        "mutating": False,
        "description": "Get all pantry inventory items with current levels and depletion estimates.",
        "parameters": _p({}),
    },
    "get_low_inventory": {
        "handler": _handle_get_low_inventory,
        "mutating": False,
        "description": "Get pantry items that are running low (below threshold percentage).",
        "parameters": _p(
            {
                "threshold": {
                    "type": "integer",
                    "description": "Level % threshold (default 20)",
                    "default": 20,
                },
            }
        ),
    },
    "list_recipes": {
        "handler": _handle_list_recipes,
        "mutating": False,
        "description": "List all saved recipes with names, tags, and ingredient counts.",
        "parameters": _p({}),
    },
    "get_recipe": {
        "handler": _handle_get_recipe,
        "mutating": False,
        "description": "Get full recipe details including ingredients and instructions.",
        "parameters": _p(
            {
                "recipe_id": {"type": "string", "description": "Recipe ID"},
            },
            ["recipe_id"],
        ),
    },
    "search_recipes": {
        "handler": _handle_search_recipes,
        "mutating": False,
        "description": "Search saved recipes by name, description, or tag.",
        "parameters": _p(
            {
                "query": {"type": "string", "description": "Search query"},
            },
            ["query"],
        ),
    },
    "list_meal_plans": {
        "handler": _handle_list_meal_plans,
        "mutating": False,
        "description": "List all meal plans with dates and plan types.",
        "parameters": _p({}),
    },
    "get_favorites_lists": {
        "handler": _handle_get_favorites_lists,
        "mutating": False,
        "description": "Get all favorites lists with their names and item counts.",
        "parameters": _p({}),
    },
    "get_favorites_items": {
        "handler": _handle_get_favorites_items,
        "mutating": False,
        "description": "Get items in a specific favorites list.",
        "parameters": _p(
            {
                "list_id": {"type": "string", "description": "Favorites list ID"},
            },
            ["list_id"],
        ),
    },
    "check_product_safety": {
        "handler": _handle_check_product_safety,
        "mutating": False,
        "description": "Check a product for harmful ingredients. Returns safety grade and flagged ingredients.",
        "parameters": _p(
            {
                "product_id": {"type": "string", "description": "Product ID to check"},
            },
            ["product_id"],
        ),
    },
    "find_deals": {
        "handler": _handle_find_deals,
        "mutating": False,
        "description": "Find products currently on sale at the store.",
        "parameters": _p(
            {
                "search_term": {"type": "string", "description": 'Search term, e.g. "chicken"'},
                "category": {
                    "type": "string",
                    "description": "Category: dairy, meat, produce, bakery, frozen, beverages",
                },
            }
        ),
    },
    "get_price_history": {
        "handler": _handle_get_price_history,
        "mutating": False,
        "description": "Get historical price data for a product to identify trends and best time to buy.",
        "parameters": _p(
            {
                "product_id": {"type": "string", "description": "Product ID"},
            },
            ["product_id"],
        ),
    },
    "get_shopping_list": {
        "handler": _handle_get_shopping_list,
        "mutating": False,
        "description": "Get the current shopping list with items, quantities, and source recipes.",
        "parameters": _p({}),
    },
    "get_cookable_recipes": {
        "handler": _handle_get_cookable_recipes,
        "mutating": False,
        "description": "Find recipes that can be made with items currently in your pantry.",
        "parameters": _p({}),
    },
    # ── Mutating (require approval) ────────────────────────────────────────
    "add_to_cart": {
        "handler": _handle_add_to_cart,
        "mutating": True,
        "description": "Add a product to the shopping cart. Requires product_id from a search.",
        "parameters": _p(
            {
                "product_id": {"type": "string", "description": "Product ID (from search results)"},
                "quantity": {
                    "type": "integer",
                    "description": "Quantity to add (default 1)",
                    "default": 1,
                },
                "modality": {
                    "type": "string",
                    "description": "PICKUP or DELIVERY",
                    "default": "PICKUP",
                },
                "description": {
                    "type": "string",
                    "description": "Product description for display",
                    "default": "",
                },
            },
            ["product_id"],
        ),
    },
    "remove_from_cart": {
        "handler": _handle_remove_from_cart,
        "mutating": True,
        "description": "Remove a product from the shopping cart.",
        "parameters": _p(
            {
                "product_id": {"type": "string", "description": "Product ID to remove"},
            },
            ["product_id"],
        ),
    },
    "clear_cart": {
        "handler": _handle_clear_cart,
        "mutating": True,
        "description": "Remove ALL items from the shopping cart.",
        "parameters": _p({}),
    },
    "add_pantry_item": {
        "handler": _handle_add_pantry_item,
        "mutating": True,
        "description": "Add a new item to pantry inventory tracking.",
        "parameters": _p(
            {
                "product_id": {"type": "string", "description": "Product ID"},
                "description": {"type": "string", "description": "Item description", "default": ""},
                "level": {
                    "type": "integer",
                    "description": "Inventory level 0-100%",
                    "default": 100,
                },
            },
            ["product_id"],
        ),
    },
    "update_pantry_level": {
        "handler": _handle_update_pantry_level,
        "mutating": True,
        "description": "Update an existing pantry item inventory level.",
        "parameters": _p(
            {
                "product_id": {"type": "string", "description": "Product ID"},
                "level_percent": {"type": "integer", "description": "New level 0-100%"},
            },
            ["product_id", "level_percent"],
        ),
    },
    "remove_pantry_item": {
        "handler": _handle_remove_pantry_item,
        "mutating": True,
        "description": "Remove an item from pantry tracking.",
        "parameters": _p(
            {
                "product_id": {"type": "string", "description": "Product ID to remove"},
            },
            ["product_id"],
        ),
    },
    "delete_recipe": {
        "handler": _handle_delete_recipe,
        "mutating": True,
        "description": "Permanently delete a saved recipe.",
        "parameters": _p(
            {
                "recipe_id": {"type": "string", "description": "Recipe ID to delete"},
            },
            ["recipe_id"],
        ),
    },
    "add_recipe_to_shopping_list": {
        "handler": _handle_add_recipe_to_shopping_list,
        "mutating": True,
        "description": "Add all ingredients from a recipe to the shopping list, scaled to servings.",
        "parameters": _p(
            {
                "recipe_id": {"type": "string", "description": "Recipe ID"},
                "servings": {
                    "type": "integer",
                    "description": "Number of servings (uses recipe default if omitted)",
                },
            },
            ["recipe_id"],
        ),
    },
    "set_servings": {
        "handler": _handle_set_servings,
        "mutating": True,
        "description": "Set the default household servings (how many people you cook for).",
        "parameters": _p(
            {
                "servings": {"type": "integer", "description": "Number of servings"},
            },
            ["servings"],
        ),
    },
}


# ---------------------------------------------------------------------------
# Build OpenAI-compatible tools array for DeepSeek
# ---------------------------------------------------------------------------


def _build_tools_array() -> list[dict[str, Any]]:
    """Build the tools array for the DeepSeek API."""
    tools = []
    for name, info in TOOL_REGISTRY.items():
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": info["description"],
                    "parameters": info["parameters"],
                },
            }
        )
    return tools


_TOOLS_ARRAY = _build_tools_array()


# ---------------------------------------------------------------------------
# Preview generation for mutating actions
# ---------------------------------------------------------------------------

_ACTION_LABELS = {
    "add_to_cart": "Add to cart",
    "remove_from_cart": "Remove from cart",
    "clear_cart": "Clear entire cart",
    "add_pantry_item": "Add to pantry",
    "update_pantry_level": "Update pantry level",
    "remove_pantry_item": "Remove from pantry",
    "delete_recipe": "Delete recipe",
    "add_recipe_to_shopping_list": "Add recipe to shopping list",
    "set_servings": "Set household servings",
}


def _generate_preview(function_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Generate a human-readable preview for a mutating action."""
    label = _ACTION_LABELS.get(function_name, function_name.replace("_", " ").title())
    details = {}

    if function_name == "add_to_cart":
        details["product_id"] = args.get("product_id", "")
        details["quantity"] = args.get("quantity", 1)
        details["modality"] = args.get("modality", "PICKUP")
        desc = args.get("description", "")
        if desc:
            details["product"] = desc

    elif function_name == "remove_from_cart":
        details["product_id"] = args.get("product_id", "")

    elif function_name == "add_pantry_item":
        details["product_id"] = args.get("product_id", "")
        details["level"] = f"{args.get('level', 100)}%"
        desc = args.get("description", "")
        if desc:
            details["item"] = desc

    elif function_name == "update_pantry_level":
        details["product_id"] = args.get("product_id", "")
        details["new_level"] = f"{args.get('level_percent', 0)}%"

    elif function_name == "remove_pantry_item":
        details["product_id"] = args.get("product_id", "")

    elif function_name == "delete_recipe":
        details["recipe_id"] = args.get("recipe_id", "")
        # Try to look up the name
        try:
            from kroger_mcp.tools.recipe_tools import _load_recipes

            data = _load_recipes()
            r = next(
                (r for r in data.get("recipes", []) if r.get("id") == args.get("recipe_id")), None
            )
            if r:
                details["recipe_name"] = r.get("name", "")
        except Exception:
            pass

    elif function_name == "add_recipe_to_shopping_list":
        details["recipe_id"] = args.get("recipe_id", "")
        if args.get("servings"):
            details["servings"] = args["servings"]
        try:
            from kroger_mcp.tools.recipe_tools import _load_recipes

            data = _load_recipes()
            r = next(
                (r for r in data.get("recipes", []) if r.get("id") == args.get("recipe_id")), None
            )
            if r:
                details["recipe_name"] = r.get("name", "")
                details["ingredient_count"] = len(r.get("ingredients", []))
        except Exception:
            pass

    elif function_name == "set_servings":
        details["servings"] = args.get("servings", 0)

    return {
        "action": label,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Conversation orchestrator
# ---------------------------------------------------------------------------


def _truncate_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep history within token limits by trimming older messages."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    # Always keep the system prompt (first message) and the most recent messages
    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    return system + non_system[-(MAX_HISTORY_MESSAGES - len(system)) :]


def process_message(
    messages: list[dict[str, Any]],
    user_message: str,
    provider: str | None = None,
) -> dict[str, Any]:
    """
    Process a chat message through the selected LLM provider with tool calling.

    Args:
        messages: Conversation history (role/content dicts).
        user_message: The new user message.
        provider: Provider id (see PROVIDER_REGISTRY); None → DEFAULT_PROVIDER.

    Returns:
        {
            response: str,              # Assistant's text reply
            messages: [...],            # Updated conversation history
            pending_action: {...}|None  # Mutating action awaiting approval
        }
    """
    client = get_client(provider)
    logger.info("process_message provider=%s model=%s", client.provider_id, client.model)

    # Build full message list with system prompt
    full_messages: list[dict[str, Any]] = []

    # Add system prompt if not already present
    has_system = any(m.get("role") == "system" for m in messages)
    if not has_system:
        full_messages.append({"role": "system", "content": SYSTEM_PROMPT})

    # Add existing history (skip any system messages we already added)
    for m in messages:
        if m.get("role") == "system" and not has_system:
            continue
        full_messages.append(m)

    # Add new user message
    full_messages.append({"role": "user", "content": user_message})

    # Truncate for token limits
    full_messages = _truncate_history(full_messages)

    # Call DeepSeek
    result = client.chat(full_messages, tools=_TOOLS_ARRAY)

    if result.get("error"):
        error_msg = result.get("message", "Unknown error")
        full_messages.append({"role": "assistant", "content": error_msg})
        return {
            "response": error_msg,
            "messages": full_messages,
            "pending_action": None,
        }

    # Parse the response
    try:
        choice = result["choices"][0]["message"]
    except (KeyError, IndexError):
        err = f"Unexpected response format from {client.label}."
        full_messages.append({"role": "assistant", "content": err})
        return {"response": err, "messages": full_messages, "pending_action": None}

    # Case 1: Plain text response (no tool call)
    if not choice.get("tool_calls"):
        content = choice.get("content", "")
        full_messages.append({"role": "assistant", "content": content})
        return {"response": content, "messages": full_messages, "pending_action": None}

    # Case 2: Tool call(s)
    # Add the assistant message with tool_calls to history
    full_messages.append(choice)

    pending_action = None
    tool_results: list[dict[str, Any]] = []

    for tool_call in choice["tool_calls"]:
        fn_name = tool_call["function"]["name"]
        try:
            fn_args = json.loads(tool_call["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            fn_args = {}

        tool_id = tool_call.get("id", f"call_{uuid.uuid4().hex[:12]}")
        tool_info = TOOL_REGISTRY.get(fn_name)

        if not tool_info:
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": json.dumps({"error": f"Unknown function: {fn_name}"}),
                }
            )
            continue

        if tool_info["mutating"]:
            # Don't execute — return as pending action
            preview = _generate_preview(fn_name, fn_args)
            pending_action = {
                "id": f"act_{uuid.uuid4().hex[:12]}",
                "function_name": fn_name,
                "args": fn_args,
                "tool_call_id": tool_id,
                "description": preview["action"],
                "preview": preview["details"],
            }
            # Add a placeholder tool result so DeepSeek knows it's pending
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": json.dumps(
                        {
                            "status": "awaiting_user_approval",
                            "action": preview["action"],
                        }
                    ),
                }
            )
        else:
            # Execute read-only tool immediately
            handler = tool_info["handler"]
            try:
                tool_result = handler(**fn_args)
            except TypeError as exc:
                tool_result = {"error": f"Invalid arguments: {str(exc)[:200]}"}
            except Exception as exc:
                tool_result = {"error": f"Tool error: {str(exc)[:200]}"}

            # Truncate large results to keep token usage manageable
            result_str = json.dumps(tool_result, default=str)
            if len(result_str) > 4000:
                result_str = result_str[:4000] + "... (truncated)"

            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": result_str,
                }
            )

    # Add tool results to history
    full_messages.extend(tool_results)

    # If there's a pending action, ask DeepSeek to describe it
    if pending_action:
        follow_up = client.chat(full_messages, tools=_TOOLS_ARRAY)
        if not follow_up.get("error"):
            try:
                follow_content = follow_up["choices"][0]["message"].get("content", "")
                if follow_content:
                    full_messages.append({"role": "assistant", "content": follow_content})
                    return {
                        "response": follow_content,
                        "messages": full_messages,
                        "pending_action": pending_action,
                    }
            except (KeyError, IndexError):
                pass
        # Fallback: generate our own description
        desc = f"I'd like to **{pending_action['description']}**. Please review and approve."
        full_messages.append({"role": "assistant", "content": desc})
        return {"response": desc, "messages": full_messages, "pending_action": pending_action}

    # No pending action — call DeepSeek again to summarize tool results
    follow_up = client.chat(full_messages, tools=_TOOLS_ARRAY)
    if follow_up.get("error"):
        fallback = "I found some results but had trouble generating a summary."
        full_messages.append({"role": "assistant", "content": fallback})
        return {"response": fallback, "messages": full_messages, "pending_action": None}

    try:
        follow_choice = follow_up["choices"][0]["message"]
    except (KeyError, IndexError):
        fallback = "Results retrieved successfully."
        full_messages.append({"role": "assistant", "content": fallback})
        return {"response": fallback, "messages": full_messages, "pending_action": None}

    # Handle case where DeepSeek wants to call another tool in the follow-up
    if follow_choice.get("tool_calls"):
        # For simplicity, just return the content if any, or a generic message
        content = follow_choice.get("content", "Here are your results.")
        full_messages.append({"role": "assistant", "content": content})
        return {"response": content, "messages": full_messages, "pending_action": None}

    content = follow_choice.get("content", "")
    full_messages.append({"role": "assistant", "content": content})
    return {"response": content, "messages": full_messages, "pending_action": None}


def execute_approved_action(function_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a previously approved mutating action.

    Returns:
        {success: bool, result: {...}, summary: str}
    """
    tool_info = TOOL_REGISTRY.get(function_name)
    if not tool_info:
        return {"success": False, "result": {}, "summary": f"Unknown function: {function_name}"}

    if not tool_info["mutating"]:
        return {"success": False, "result": {}, "summary": "This action does not require approval."}

    handler = tool_info["handler"]
    try:
        result = handler(**args)
    except TypeError as exc:
        return {"success": False, "result": {}, "summary": f"Invalid arguments: {str(exc)[:200]}"}
    except Exception as exc:
        return {"success": False, "result": {}, "summary": f"Action failed: {str(exc)[:200]}"}

    if result.get("error"):
        return {"success": False, "result": result, "summary": result["error"]}

    # Generate a human-readable summary
    label = _ACTION_LABELS.get(function_name, function_name)
    summary = f"{label} completed successfully."

    if function_name == "add_to_cart":
        desc = args.get("description", args.get("product_id", ""))
        qty = args.get("quantity", 1)
        summary = f"Added {desc} (x{qty}) to your cart."
    elif function_name == "remove_from_cart":
        summary = "Removed item from your cart."
    elif function_name == "clear_cart":
        summary = "Cart has been cleared."
    elif function_name == "add_pantry_item":
        desc = args.get("description", args.get("product_id", ""))
        summary = f'Added {desc} to pantry at {args.get("level", 100)}%.'
    elif function_name == "update_pantry_level":
        summary = f'Updated pantry level to {args.get("level_percent", 0)}%.'
    elif function_name == "remove_pantry_item":
        summary = "Removed item from pantry."
    elif function_name == "delete_recipe":
        summary = "Recipe deleted."
    elif function_name == "add_recipe_to_shopping_list":
        added = result.get("added", 0)
        name = result.get("recipe", "Recipe")
        summary = f'Added {added} ingredients from "{name}" to your shopping list.'
    elif function_name == "set_servings":
        summary = f'Household servings set to {args.get("servings", 0)}.'

    return {"success": True, "result": result, "summary": summary}
