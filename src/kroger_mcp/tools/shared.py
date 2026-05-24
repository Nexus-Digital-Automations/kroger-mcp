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

from dotenv import load_dotenv
from kroger_api.kroger_api import KrogerAPI
from kroger_api.token_storage import load_token
from kroger_api.utils.env import get_zip_code, load_and_validate_env

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Global state for clients and preferred location
_authenticated_client: KrogerAPI | None = None
_client_credentials_client: KrogerAPI | None = None

def get_client_credentials_client() -> KrogerAPI:
    """Get or create a client credentials authenticated client for public data"""
    global _client_credentials_client

    if _client_credentials_client is not None and _client_credentials_client.test_current_token():
        return _client_credentials_client

    _client_credentials_client = None

    try:
        load_and_validate_env(["KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET"])
        _client_credentials_client = KrogerAPI()

        # Try to load existing token first.
        # bandit B105 flags the variable name; this is a filename, not a credential.
        token_file = ".kroger_token_client_product.compact.json"  # nosec B105
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

        # Try to load existing user token first.
        # bandit B105 flags the variable name; this is a filename, not a credential.
        token_file = ".kroger_token_user.json"  # nosec B105
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


def _resolve_pref_user_id(user_id: str | None) -> str:
    """Resolve user_id for preference reads/writes.

    Falls back to mcp_user_id() when None — keeps existing callers working
    while still scoping to the per-Claude-Desktop user when KROGER_MCP_USER_ID
    is set. HTTP-route callers should pass user_id explicitly.
    """
    from kroger_mcp.auth.dependencies import mcp_user_id

    return user_id if user_id is not None else mcp_user_id()


def _load_preferences(user_id: str | None = None) -> dict:
    """Load this user's preferences from the user_settings table."""
    from kroger_mcp.analytics.database import get_db_connection

    owner = _resolve_pref_user_id(user_id)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT setting_key, setting_value FROM user_settings WHERE user_id = ?",
            (owner,),
        ).fetchall()
        prefs: dict = {}
        for row in rows:
            value = row["setting_value"]
            # Coerce stringly-stored numbers + booleans back to native types
            if value in ("True", "true"):
                value = True
            elif value in ("False", "false"):
                value = False
            elif value is not None and value.isdigit():
                value = int(value)
            prefs[row["setting_key"]] = value
        return prefs
    finally:
        conn.close()


def _save_preference(key: str, value, user_id: str | None = None) -> None:
    """Upsert one preference key/value for the given user."""
    from kroger_mcp.analytics.database import get_db_connection

    owner = _resolve_pref_user_id(user_id)
    stored = None if value is None else str(value)
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, setting_key, setting_value, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at
            """,
            (owner, key, stored),
        )
        conn.commit()
    finally:
        conn.close()


def get_preferred_location_id(user_id: str | None = None) -> str | None:
    """Per-user preferred location ID; falls back to KROGER_LOCATION_ID env."""
    preferences = _load_preferences(user_id=user_id)
    return preferences.get("preferred_location_id") or os.environ.get("KROGER_LOCATION_ID")


def set_preferred_location_id(location_id: str, user_id: str | None = None) -> None:
    """Persist this user's preferred location ID."""
    _save_preference("preferred_location_id", location_id, user_id=user_id)


def format_currency(value: float | None) -> str:
    """Format a value as currency"""
    if value is None:
        return "N/A"
    return f"${value:.2f}"


def get_default_zip_code() -> str:
    """Get the default zip code from environment or fallback"""
    return get_zip_code(default="10001")


def get_default_servings(user_id: str | None = None) -> int:
    """This user's default servings per meal (household size). Defaults to 4."""
    preferences = _load_preferences(user_id=user_id)
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


def set_default_servings(servings: int, user_id: str | None = None) -> None:
    """Persist this user's default servings per meal (household size).

    Raises ValueError if servings is outside 1..20.
    """
    if not 1 <= servings <= 20:
        raise ValueError("Servings must be between 1 and 20")
    _save_preference("default_servings_per_meal", servings, user_id=user_id)


def get_include_spices_by_default(user_id: str | None = None) -> bool:
    """Whether the Send-to-Kroger-Cart preview should pre-check spice items.

    Defaults to False — spices appear in the preview but stay unchecked until
    the user explicitly opts them in, keeping pantry seasonings off the cart.
    """
    return bool(_load_preferences(user_id=user_id).get("include_spices_by_default", False))


def set_include_spices_by_default(value: bool, user_id: str | None = None) -> None:
    """Persist this user's 'include spices by default' Advanced-Settings toggle."""
    _save_preference("include_spices_by_default", bool(value), user_id=user_id)


def get_kroger_credentials(user_id: str | None = None) -> dict:
    """Get this user's Kroger API credentials; falls back to KROGER_* env vars."""
    import json as _json

    preferences = _load_preferences(user_id=user_id)
    creds_raw = preferences.get("kroger_credentials")
    creds = _json.loads(creds_raw) if isinstance(creds_raw, str) else (creds_raw or {})
    return {
        "client_id": creds.get("client_id") or os.environ.get("KROGER_CLIENT_ID", ""),
        "client_secret": creds.get("client_secret") or os.environ.get("KROGER_CLIENT_SECRET", ""),
        "redirect_uri": creds.get("redirect_uri") or os.environ.get("KROGER_REDIRECT_URI", ""),
    }


def set_kroger_credentials(
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str | None = None,
    user_id: str | None = None,
) -> None:
    """Save Kroger API credentials per-user."""
    import json as _json

    prefs = _load_preferences(user_id=user_id)
    existing_raw = prefs.get("kroger_credentials")
    existing = _json.loads(existing_raw) if isinstance(existing_raw, str) else (existing_raw or {})
    if client_id is not None:
        existing["client_id"] = client_id
    if client_secret is not None:
        existing["client_secret"] = client_secret
    if redirect_uri is not None:
        existing["redirect_uri"] = redirect_uri
    _save_preference("kroger_credentials", _json.dumps(existing), user_id=user_id)


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


def get_product_sort_preferences(user_id: str | None = None) -> dict:
    """Get this user's saved product page sort preferences."""
    import json as _json

    preferences = _load_preferences(user_id=user_id)
    raw = preferences.get("product_sort")
    if isinstance(raw, str):
        return _json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {
        "search_sort_stack": ["favorites"],
        "deals_sort_stack": [],
    }


def set_product_sort_preferences(
    search_sort_stack: list,
    deals_sort_stack: list,
    user_id: str | None = None,
) -> None:
    """Save this user's product page sort preferences."""
    import json as _json

    valid_keys = {"favorites", "health", "price", "percent", "dollar"}
    search_sort_stack = [k for k in search_sort_stack if k in valid_keys]
    deals_sort_stack = [k for k in deals_sort_stack if k in valid_keys]
    _save_preference(
        "product_sort",
        _json.dumps(
            {
                "search_sort_stack": search_sort_stack,
                "deals_sort_stack": deals_sort_stack,
            }
        ),
        user_id=user_id,
    )
