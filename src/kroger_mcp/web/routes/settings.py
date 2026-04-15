"""Settings page route."""
import json
import pathlib
import tempfile
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from kroger_mcp.tools.shared import get_preferred_location_id, get_default_servings
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()

# Same state file as api/settings.py uses
_WEB_OAUTH_STATE_FILE = pathlib.Path(tempfile.gettempdir()) / "kroger_web_oauth_state.json"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    oauth: Optional[str] = Query(default=None),
    detail: Optional[str] = Query(default=None),
):
    location_id = get_preferred_location_id() or ""
    servings = get_default_servings()

    # Check auth status
    auth_status = "not_configured"
    try:
        from kroger_mcp.tools.shared import get_authenticated_client
        get_authenticated_client()
        auth_status = "authenticated"
    except Exception as exc:
        if "Authentication required" in str(exc):
            auth_status = "not_authenticated"
        else:
            auth_status = "not_configured"

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "active_page": "settings",
        "location_id": location_id,
        "servings": servings,
        "auth_status": auth_status,
        "oauth_result": oauth or "",
        "oauth_detail": detail or "",
    })


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    """Handle Kroger OAuth callback — exchange code for token, redirect to settings."""
    from kroger_api import KrogerAPI
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

    # Exchange authorization code for token
    creds = get_kroger_credentials()
    redirect_uri = saved.get("redirect_uri", "http://localhost:8000/callback")

    try:
        kroger = KrogerAPI(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            redirect_uri=redirect_uri,
        )
        kroger.client.get_token_with_authorization_code(
            code,
            code_verifier=saved["pkce_params"]["code_verifier"],
        )
    except Exception:
        return RedirectResponse(url="/settings?oauth=error&detail=token_exchange_failed")

    # Clean up state file
    _WEB_OAUTH_STATE_FILE.unlink(missing_ok=True)

    # Invalidate cached client so it reloads from the new token file
    invalidate_authenticated_client()

    return RedirectResponse(url="/settings?oauth=success")
