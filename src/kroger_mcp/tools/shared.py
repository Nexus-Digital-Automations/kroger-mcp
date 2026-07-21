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
import hashlib
import json
import logging
import os
import threading

from dotenv import load_dotenv
from kroger_api.kroger_api import KrogerAPI
from kroger_api.token_storage import load_token
from kroger_api.utils.env import get_zip_code

from kroger_mcp.tools._kroger_retry import install_kroger_retry

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Install 429/5xx backoff on the Kroger HTTP chokepoint before any client is
# built. Idempotent; covers every client created below.
install_kroger_retry()

# App-level (client-credentials) clients, cached PER client_id.
#
# Hybrid model: most users share the env app's client_id (one cached client);
# a power user who brings their own client_id gets a SEPARATE cached client so
# their public reads run under — and are rate-limited against — their own
# Kroger app. Keyed by client_id (not user) so users sharing the env app share
# one client. Guarded by a lock because builders run in a thread pool.
#
# The per-user *authenticated* client is intentionally NOT cached: with
# concurrent web users a shared global (and a shared token file) let one user's
# Kroger OAuth overwrite another's. Per-user tokens live encrypted in the
# ``kroger_tokens`` table (see auth/kroger_tokens.py); a FRESH KrogerAPI() is
# built per call.
_cc_clients: dict[str, KrogerAPI] = {}
_cc_clients_lock = threading.Lock()


def kroger_cache_key(client: KrogerAPI, kind: str, **params: object) -> str:
    """Build a Redis key for a public Kroger read, scoped to the app's client_id.

    Including the client_id keeps a power user's cached results isolated from
    the shared app's (no cross-tenant leak) while letting everyone on the shared
    env app reuse warm entries.
    """
    raw_id = getattr(client.client, "client_id", "") or ""
    cid = hashlib.sha256(raw_id.encode()).hexdigest()[:12]
    norm = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] is not None)
    return f"kapi:{kind}:{cid}:{norm}"


def _cc_token_file(client_id: str) -> str:
    """Per-client_id cache file for the client-credentials token.

    Keying by a hash of the client_id keeps power users' app tokens from
    colliding in one shared file. bandit B105 flags the literal; it is a
    filename, not a credential.
    """
    digest = hashlib.sha256(client_id.encode()).hexdigest()[:12]
    return f".kroger_token_cc_{digest}.json"  # nosec B105


def get_client_credentials_client(user_id: str) -> KrogerAPI:
    """Get/create a client-credentials client for public data, scoped to the caller's app.

    Resolves the caller's Kroger ``client_id``/``client_secret`` (per-user
    preference → ``KROGER_*`` env fallback) and returns a client cached under
    that ``client_id``. ``user_id=None`` resolves via ``mcp_user_id()`` so
    existing MCP callers are unchanged.
    """
    creds = get_kroger_credentials(user_id=_resolve_pref_user_id(user_id))
    client_id = creds["client_id"]
    client_secret = creds["client_secret"]
    if not client_id or not client_secret:
        raise Exception(
            "Kroger credentials not configured. Set KROGER_CLIENT_ID/SECRET or add them "
            "in Advanced Settings."
        )

    with _cc_clients_lock:
        cached = _cc_clients.get(client_id)
        if cached is not None and cached.test_current_token():
            return cached

        try:
            client = KrogerAPI(client_id=client_id, client_secret=client_secret)

            token_file = _cc_token_file(client_id)
            token_info = load_token(token_file)
            if token_info:
                client.client.token_info = token_info
                if client.test_current_token():
                    _cc_clients[client_id] = client
                    return client

            client.authorization.get_token_with_client_credentials("product.compact")
            _cc_clients[client_id] = client
            return client
        except Exception as e:
            raise Exception(f"Failed to get client credentials: {str(e)}") from e


def get_authenticated_client(user_id: str) -> KrogerAPI:
    """Get a user-authenticated Kroger client for cart/account operations.

    Loads the caller's per-user token from the encrypted ``kroger_tokens`` table
    (NOT the legacy shared ``.kroger_token_user.json`` file) and builds a FRESH
    ``KrogerAPI()`` per call so concurrent users never share auth state.

    Args:
        user_id: The user to authenticate as. When ``None`` (the default — used
            by every existing MCP tool caller), it is resolved via
            ``mcp_user_id()`` so those callers keep working unchanged. Web routes
            pass the logged-in user's id explicitly.

    Returns:
        KrogerAPI: Authenticated client.

    Raises:
        Exception: If no valid token is available and authentication is required.
    """
    # Imported lazily to avoid an import cycle (auth.dependencies → fastapi, etc.).
    from kroger_mcp.auth.dependencies import mcp_user_id
    from kroger_mcp.auth.kroger_tokens import load_kroger_token, save_kroger_token

    resolved_user_id = mcp_user_id() if user_id is None else user_id

    try:
        # Build the client from THIS user's credentials (per-user preference →
        # env fallback) so a power user's token refresh runs under the same
        # client_id that minted it, not the env app's.
        creds = get_kroger_credentials(user_id=resolved_user_id)
        if not creds["client_id"] or not creds["client_secret"]:
            raise Exception(
                "Authentication required. Run auth(action='start') to begin the OAuth flow, "
                "then auth(action='complete', redirect_url=...) to finish it."
            )

        token_info = load_kroger_token(resolved_user_id)

        if token_info:
            # Build a fresh client; do NOT set client.token_file — leaving it
            # None keeps kroger-api from writing refreshes to the shared file.
            client = KrogerAPI(
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
                redirect_uri=creds["redirect_uri"] or None,
            )
            client.client.token_info = token_info

            if client.test_current_token():
                # test_current_token() may have silently refreshed the token in
                # place (kroger-api auto-refreshes on a failed probe). If so, the
                # in-memory token_info changed — persist it back per-user.
                refreshed = client.client.token_info
                if refreshed and refreshed.get("access_token") != token_info.get("access_token"):
                    save_kroger_token(resolved_user_id, refreshed)
                return client

            # Token invalid; try an explicit refresh and persist on success.
            if "refresh_token" in token_info:
                try:
                    client.authorization.refresh_token(token_info["refresh_token"])
                    if client.test_current_token():
                        save_kroger_token(resolved_user_id, client.client.token_info)
                        return client
                except Exception:
                    # Refresh failed → fall through to "Authentication required".
                    logger.warning(
                        "kroger token refresh failed for user=%s; re-auth required",
                        resolved_user_id,
                    )

        # No usable token → user-initiated authentication required.
        raise Exception(
            "Authentication required. Run auth(action='start') to begin the OAuth flow, "
            "then auth(action='complete', redirect_url=...) to finish it."
        )
    except Exception as e:
        if "Authentication required" in str(e):
            # Expected error when authentication is needed.
            raise
        # Other unexpected errors.
        raise Exception(f"Authentication failed: {str(e)}") from e


def invalidate_authenticated_client(user_id: str) -> None:
    """Force re-authentication for a user by deleting their stored token.

    Best-effort: any failure to delete is swallowed (the next auth attempt
    re-fetches a token anyway). ``user_id=None`` resolves via ``mcp_user_id()``
    so existing MCP callers keep working unchanged.
    """
    from kroger_mcp.auth.dependencies import mcp_user_id
    from kroger_mcp.auth.kroger_tokens import delete_kroger_token

    resolved_user_id = mcp_user_id() if user_id is None else user_id
    try:
        delete_kroger_token(resolved_user_id)
    except Exception:
        logger.warning(
            "could not delete kroger token for user=%s during invalidate",
            resolved_user_id,
        )


def invalidate_client_credentials_client(user_id: str) -> None:
    """Drop cached client-credentials client(s) to force a fresh token.

    With ``user_id`` (or its resolved default), only that caller's app client
    (keyed by their client_id) is evicted — a power user changing credentials
    does not disturb everyone else on the shared env app. Without a resolvable
    client_id, evict all (used by broad MCP-side resets).
    """
    try:
        creds = get_kroger_credentials(user_id=_resolve_pref_user_id(user_id))
        client_id = creds.get("client_id")
    except Exception:
        client_id = None

    with _cc_clients_lock:
        if client_id and client_id in _cc_clients:
            del _cc_clients[client_id]
        elif not client_id:
            _cc_clients.clear()


def _resolve_pref_user_id(user_id: str | None) -> str:
    """Resolve user_id for preference reads/writes.

    Falls back to mcp_user_id() when None — keeps existing callers working
    while still scoping to the per-Claude-Desktop user when KROGER_MCP_USER_ID
    is set. HTTP-route callers should pass user_id explicitly.
    """
    from kroger_mcp.auth.dependencies import mcp_user_id

    return user_id if user_id is not None else mcp_user_id()


def _load_preferences(user_id: str) -> dict:
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


def _save_preference(key: str, value, user_id: str) -> None:
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


def get_preferred_location_id(user_id: str) -> str | None:
    """Per-user preferred location ID; falls back to KROGER_LOCATION_ID env."""
    preferences = _load_preferences(user_id=user_id)
    return preferences.get("preferred_location_id") or os.environ.get("KROGER_LOCATION_ID")


def set_preferred_location_id(location_id: str, user_id: str) -> None:
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


def get_default_servings(user_id: str) -> int:
    """This user's default servings per meal (household size). Defaults to 4."""
    preferences = _load_preferences(user_id=user_id)
    return preferences.get("default_servings_per_meal", 4)


def set_default_servings(servings: int, user_id: str) -> None:
    """Persist this user's default servings per meal (household size).

    Raises ValueError if servings is outside 1..20.
    """
    if not 1 <= servings <= 20:
        raise ValueError("Servings must be between 1 and 20")
    _save_preference("default_servings_per_meal", servings, user_id=user_id)


def get_include_spices_by_default(user_id: str) -> bool:
    """Whether the Send-to-Kroger-Cart preview should pre-check spice items.

    Defaults to False — spices appear in the preview but stay unchecked until
    the user explicitly opts them in, keeping pantry seasonings off the cart.
    """
    return bool(_load_preferences(user_id=user_id).get("include_spices_by_default", False))


def set_include_spices_by_default(value: bool, user_id: str) -> None:
    """Persist this user's 'include spices by default' Advanced-Settings toggle."""
    _save_preference("include_spices_by_default", bool(value), user_id=user_id)


def get_favorites_display_mode(user_id: str) -> str:
    """How favorites-on-sale are surfaced on the Deals tab.

    'sort' (default) — favorites-on-sale stay informational; the user opts
    into seeing them first via the existing "Favorites" sort-rank option.
    'section' — favorites-on-sale are pulled into their own dedicated
    section above the main deals grid and excluded from the grid itself.
    """
    return str(_load_preferences(user_id=user_id).get("favorites_display_mode", "sort"))


def set_favorites_display_mode(value: str, user_id: str) -> None:
    """Persist this user's favorites-on-sale display mode.

    Raises ValueError if value isn't 'sort' or 'section'.
    """
    if value not in ("sort", "section"):
        raise ValueError("favorites_display_mode must be 'sort' or 'section'")
    _save_preference("favorites_display_mode", value, user_id=user_id)


def get_meal_plan_pantry_deduction_mode(user_id: str) -> str:
    """Whether past meal-plan entries auto-deduct pantry or wait for confirmation.

    'automatic' (default) — past meals deduct pantry the moment their date
    passes, no confirmation required.
    'confirm' — past, un-cooked meals surface in the notification bell as
    "pending" and only deduct pantry once the user confirms them.
    """
    return str(
        _load_preferences(user_id=user_id).get("meal_plan_pantry_deduction_mode", "automatic")
    )


def set_meal_plan_pantry_deduction_mode(value: str, user_id: str) -> None:
    """Persist this user's meal-plan pantry deduction mode.

    Raises ValueError if value isn't 'automatic' or 'confirm'.
    """
    if value not in ("automatic", "confirm"):
        raise ValueError("meal_plan_pantry_deduction_mode must be 'automatic' or 'confirm'")
    _save_preference("meal_plan_pantry_deduction_mode", value, user_id=user_id)


def should_show_deduction_default_notice(user_id: str) -> bool:
    """Whether to show the one-time "pantry now auto-deducts by default" toast.

    True only for users relying on the (now-'automatic') default — i.e. who
    never explicitly chose a mode — and who haven't dismissed the notice yet.
    Users who explicitly picked 'confirm' or 'automatic' see nothing; their
    choice already reflects a deliberate decision.
    """
    prefs = _load_preferences(user_id=user_id)
    has_explicit_mode = "meal_plan_pantry_deduction_mode" in prefs
    notice_seen = bool(prefs.get("meal_plan_deduction_notice_seen", False))
    return not has_explicit_mode and not notice_seen


def mark_deduction_default_notice_seen(user_id: str) -> None:
    """Persist that this user has dismissed the deduction-default notice."""
    _save_preference("meal_plan_deduction_notice_seen", True, user_id=user_id)


def get_kroger_credentials(user_id: str) -> dict:
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
    *, user_id: str,
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


def get_product_sort_preferences(user_id: str) -> dict:
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
    user_id: str,
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
