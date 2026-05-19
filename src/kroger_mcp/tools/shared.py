"""
Shared utilities and client management for Kroger MCP server.

Owns:
  - Kroger API client lifecycle (creation, caching, invalidation)
  - Credential resolution (preferences file → env vars)
  - User preferences persistence (location, servings, sort, credentials)
  - Token file access (read/delete) for the web OAuth flow

Does NOT own:
  - OAuth flow orchestration (see web/routes/settings.py and tools/auth_tools.py)
  - Kroger API calls (see tools/*_tools.py)
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from kroger_api.kroger_api import KrogerAPI
from kroger_api.token_storage import load_token
from kroger_api.utils.env import get_zip_code, load_and_validate_env

from ._storage import JsonStore

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Global state for clients and preferred location
_authenticated_client: KrogerAPI | None = None
_client_credentials_client: KrogerAPI | None = None

# JSON files for configuration storage
_BASE_DIR = Path(__file__).parent.parent.parent.parent  # → kroger-mcp/
PREFERENCES_FILE = str(_BASE_DIR / "kroger_preferences.json")


def get_client_credentials_client() -> KrogerAPI:
    """Get or create a client credentials authenticated client for public data"""
    global _client_credentials_client

    if _client_credentials_client is not None and _client_credentials_client.test_current_token():
        return _client_credentials_client

    _client_credentials_client = None

    try:
        load_and_validate_env(["KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET"])
        _client_credentials_client = KrogerAPI()

        # Try to load existing token first
        token_file = ".kroger_token_client_product.compact.json"
        token_info = load_token(token_file)

        if token_info:
            # Test if the token is still valid
            _client_credentials_client.client.token_info = token_info
            if _client_credentials_client.test_current_token():
                # Token is valid, use it
                return _client_credentials_client

        # Token is invalid or not found, get a new one
        token_info = _client_credentials_client.authorization.get_token_with_client_credentials(
            "product.compact"
        )
        return _client_credentials_client
    except Exception as e:
        raise Exception(f"Failed to get client credentials: {str(e)}") from e


def get_authenticated_client() -> KrogerAPI:
    """Get or create a user-authenticated client for cart operations

    This function attempts to load an existing token or prompts for authentication.
    In an MCP context, the user needs to explicitly call start_authentication and
    complete_authentication tools to authenticate.

    Returns:
        KrogerAPI: Authenticated client

    Raises:
        Exception: If no valid token is available and authentication is required
    """
    global _authenticated_client

    if _authenticated_client is not None and _authenticated_client.test_current_token():
        # Client exists and token is still valid
        return _authenticated_client

    # Clear the reference if token is invalid
    _authenticated_client = None

    try:
        load_and_validate_env(["KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET", "KROGER_REDIRECT_URI"])

        # Try to load existing user token first
        token_file = ".kroger_token_user.json"
        token_info = load_token(token_file)

        if token_info:
            # Create a new client with the loaded token
            _authenticated_client = KrogerAPI()
            _authenticated_client.client.token_info = token_info
            _authenticated_client.client.token_file = token_file

            if _authenticated_client.test_current_token():
                # Token is valid, use it
                return _authenticated_client

            # Token is invalid, try to refresh it
            if "refresh_token" in token_info:
                try:
                    _authenticated_client.authorization.refresh_token(token_info["refresh_token"])
                    # If refresh was successful, return the client
                    if _authenticated_client.test_current_token():
                        return _authenticated_client
                except Exception:
                    # Refresh failed, need to re-authenticate
                    _authenticated_client = None

        # No valid token available, need user-initiated authentication
        raise Exception(
            "Authentication required. Please use the start_authentication tool to begin the OAuth flow, "
            "then complete it with the complete_authentication tool."
        )
    except Exception as e:
        if "Authentication required" in str(e):
            # This is an expected error when authentication is needed
            raise
        else:
            # Other unexpected errors
            raise Exception(f"Authentication failed: {str(e)}") from e


def invalidate_authenticated_client():
    """Invalidate the authenticated client to force re-authentication"""
    global _authenticated_client
    _authenticated_client = None


def invalidate_client_credentials_client():
    """Invalidate the client credentials client to force re-authentication"""
    global _client_credentials_client
    _client_credentials_client = None


_preferences_store = JsonStore(PREFERENCES_FILE, default=lambda: {"preferred_location_id": None})


def _load_preferences() -> dict:
    return _preferences_store.load()


def _save_preferences(preferences: dict) -> None:
    _preferences_store.save(preferences)


def get_preferred_location_id() -> str | None:
    """Get preferred location ID: preferences file first, then KROGER_LOCATION_ID env var"""
    preferences = _load_preferences()
    return preferences.get("preferred_location_id") or os.environ.get("KROGER_LOCATION_ID")


def set_preferred_location_id(location_id: str) -> None:
    """Set the preferred location ID in preferences file"""
    preferences = _load_preferences()
    preferences["preferred_location_id"] = location_id
    _save_preferences(preferences)


def format_currency(value: float | None) -> str:
    """Format a value as currency"""
    if value is None:
        return "N/A"
    return f"${value:.2f}"


def get_default_zip_code() -> str:
    """Get the default zip code from environment or fallback"""
    return get_zip_code(default="10001")


def get_default_servings() -> int:
    """
    Get the user's default servings per meal preference.

    Returns the household size/serving preference for recipes.
    Defaults to 4 if not set.

    Returns:
        Number of servings (1-20)
    """
    preferences = _load_preferences()
    return preferences.get("default_servings_per_meal", 4)


# ==================== ASYNC WRAPPERS ====================
# The Kroger API client uses synchronous HTTP (requests library). Calling it
# directly from an async tool handler blocks the event loop for the duration of
# the network round-trip. Use these wrappers from async handlers instead.


async def async_get_client_credentials_client() -> KrogerAPI:
    """Async wrapper for get_client_credentials_client() — runs in thread pool."""
    return await asyncio.to_thread(get_client_credentials_client)


async def async_get_authenticated_client() -> KrogerAPI:
    """Async wrapper for get_authenticated_client() — runs in thread pool."""
    return await asyncio.to_thread(get_authenticated_client)


def set_default_servings(servings: int) -> None:
    """
    Set the user's default servings per meal preference.

    This preference is used when:
    - Creating new recipes (if servings not specified)
    - Adding recipes to shopping list (if override not specified)
    - Assigning meals to meal plan (if override not specified)
    - Displaying recipe information

    Args:
        servings: Number of servings (1-20)

    Raises:
        ValueError: If servings is not between 1 and 20
    """
    if not 1 <= servings <= 20:
        raise ValueError("Servings must be between 1 and 20")

    preferences = _load_preferences()
    preferences["default_servings_per_meal"] = servings
    _save_preferences(preferences)


def get_include_spices_by_default() -> bool:
    """Whether the Send-to-Kroger-Cart preview should pre-check spice items.

    Defaults to False — spices appear in the preview but stay unchecked until
    the user explicitly opts them in, keeping pantry seasonings off the cart.
    """
    return bool(_load_preferences().get("include_spices_by_default", False))


def set_include_spices_by_default(value: bool) -> None:
    """Persist the 'include spices by default' Advanced-Settings toggle."""
    preferences = _load_preferences()
    preferences["include_spices_by_default"] = bool(value)
    _save_preferences(preferences)


def get_kroger_credentials() -> dict:
    """Get Kroger API credentials: preferences first, then env vars."""
    preferences = _load_preferences()
    creds = preferences.get("kroger_credentials", {})
    return {
        "client_id": creds.get("client_id") or os.environ.get("KROGER_CLIENT_ID", ""),
        "client_secret": creds.get("client_secret") or os.environ.get("KROGER_CLIENT_SECRET", ""),
        "redirect_uri": creds.get("redirect_uri") or os.environ.get("KROGER_REDIRECT_URI", ""),
    }


def set_kroger_credentials(
    client_id: str = None,
    client_secret: str = None,
    redirect_uri: str = None,
) -> None:
    """Save Kroger API credentials to preferences file."""
    preferences = _load_preferences()
    existing = preferences.get("kroger_credentials", {})
    if client_id is not None:
        existing["client_id"] = client_id
    if client_secret is not None:
        existing["client_secret"] = client_secret
    if redirect_uri is not None:
        existing["redirect_uri"] = redirect_uri
    preferences["kroger_credentials"] = existing
    _save_preferences(preferences)


def get_token_info() -> dict | None:
    """Load the user token file and return its contents, or None if missing.

    Raises no exceptions — file-not-found and parse errors are logged and
    return None so callers can treat a missing/corrupt token as "not authenticated".
    """
    try:
        return load_token(".kroger_token_user.json")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load user token file: %s", exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error loading user token file: %s", exc)
        return None


def delete_user_token() -> None:
    """Delete the user token file and invalidate the cached client.

    Raises:
        OSError: If the token file exists but cannot be deleted (permissions).
    """
    from kroger_api.token_storage import clear_token

    clear_token(".kroger_token_user.json")
    invalidate_authenticated_client()


def get_product_sort_preferences() -> dict:
    """Get saved product page sort preferences."""
    preferences = _load_preferences()
    return preferences.get(
        "product_sort",
        {
            "search_sort_stack": ["favorites"],
            "deals_sort_stack": [],
        },
    )


def set_product_sort_preferences(search_sort_stack: list, deals_sort_stack: list) -> None:
    """Save product page sort preferences."""
    valid_keys = {"favorites", "health", "price", "percent", "dollar"}
    search_sort_stack = [k for k in search_sort_stack if k in valid_keys]
    deals_sort_stack = [k for k in deals_sort_stack if k in valid_keys]
    preferences = _load_preferences()
    preferences["product_sort"] = {
        "search_sort_stack": search_sort_stack,
        "deals_sort_stack": deals_sort_stack,
    }
    _save_preferences(preferences)
