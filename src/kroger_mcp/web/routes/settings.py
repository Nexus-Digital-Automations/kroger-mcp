"""Settings page route."""

import json
import pathlib
import tempfile
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.tools.shared import (
    get_default_servings,
    get_preferred_location_id,
)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()

# Same state file as api/settings.py uses
_WEB_OAUTH_STATE_FILE = pathlib.Path(tempfile.gettempdir()) / "kroger_web_oauth_state.json"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    oauth: str | None = Query(default=None),
    detail: str | None = Query(default=None),
):
    from kroger_mcp.tools.shared import get_include_spices_by_default

    location_id = get_preferred_location_id() or ""
    servings = get_default_servings()
    include_spices_by_default = get_include_spices_by_default()

    # Check auth status
    auth_status = "not_configured"
    try:
        from kroger_mcp.tools.shared import get_authenticated_client

        get_authenticated_client(current_user_id(request))
        auth_status = "authenticated"
    except Exception as exc:
        if "Authentication required" in str(exc):
            auth_status = "not_authenticated"
        else:
            auth_status = "not_configured"

    user = getattr(request.state, "user", None)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active_page": "settings",
            "location_id": location_id,
            "servings": servings,
            "include_spices_by_default": include_spices_by_default,
            "auth_status": auth_status,
            "oauth_result": oauth or "",
            "oauth_detail": detail or "",
            "user": user,
        },
    )


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """Handle Kroger OAuth callback — exchange code for token, redirect to settings."""
    from kroger_api import KrogerAPI

    from kroger_mcp.auth.kroger_tokens import save_kroger_token
    from kroger_mcp.tools.shared import (
        get_kroger_credentials,
        invalidate_authenticated_client,
    )

    if error:
        return RedirectResponse(url=f"/settings?oauth=error&detail={error}")

    if not code:
        return RedirectResponse(url="/settings?oauth=error&detail=no_code")

    # Load persisted PKCE state
    if not _WEB_OAUTH_STATE_FILE.exists():
        return RedirectResponse(url="/settings?oauth=error&detail=no_state")

    try:
        saved = json.loads(_WEB_OAUTH_STATE_FILE.read_text())
    except Exception:
        return RedirectResponse(url="/settings?oauth=error&detail=bad_state")

    # Verify state parameter (CSRF protection)
    if state != saved.get("state"):
        return RedirectResponse(url="/settings?oauth=error&detail=state_mismatch")

    # Exchange authorization code for token (scoped to the logged-in user so a
    # power user's own client_id/secret mints the token, not the env app's).
    creds = get_kroger_credentials(user_id=current_user_id(request))
    redirect_uri = saved.get("redirect_uri", "http://localhost:8000/callback")

    try:
        kroger = KrogerAPI(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            redirect_uri=redirect_uri,
        )
        token_info = kroger.client.get_token_with_authorization_code(
            code,
            code_verifier=saved["pkce_params"]["code_verifier"],
        )
    except Exception:
        return RedirectResponse(url="/settings?oauth=error&detail=token_exchange_failed")

    # Persist the new token per-user in the encrypted kroger_tokens table. This
    # is the source of truth get_authenticated_client() reads — the legacy
    # ``.kroger_token_user.json`` file kroger-api also wrote is no longer used.
    try:
        user_id = current_user_id(request)
        save_kroger_token(user_id, token_info)
    except Exception:
        return RedirectResponse(url="/settings?oauth=error&detail=token_persist_failed")

    # Clean up state file
    _WEB_OAUTH_STATE_FILE.unlink(missing_ok=True)

    # Drop any stale token so the next call reloads the freshly stored one.
    invalidate_authenticated_client(user_id)

    return RedirectResponse(url="/settings?oauth=success")
