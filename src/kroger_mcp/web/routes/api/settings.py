"""API routes for settings management."""

import json
import logging
import pathlib
import tempfile

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.auth.dependencies import current_user_id

logger = logging.getLogger(__name__)

router = APIRouter()

# Web OAuth state file — separate from MCP flow to avoid conflicts
_WEB_OAUTH_STATE_FILE = pathlib.Path(tempfile.gettempdir()) / "kroger_web_oauth_state.json"


class ServingsBody(BaseModel):
    servings: int


class IncludeSpicesBody(BaseModel):
    include: bool


class LocationBody(BaseModel):
    location_id: str


class CredentialsBody(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""


@router.get("/api/settings")
async def get_settings(request: Request):
    """Return current app settings for the authenticated user."""
    try:
        from kroger_mcp.tools.shared import (
            get_authenticated_client,
            get_default_servings,
            get_include_spices_by_default,
            get_preferred_location_id,
        )

        user_id = current_user_id(request)
        location_id = get_preferred_location_id(user_id=user_id) or ""
        servings = get_default_servings(user_id=user_id)
        include_spices_by_default = get_include_spices_by_default(user_id=user_id)

        auth_status = "not_configured"
        try:
            get_authenticated_client()
            auth_status = "authenticated"
        except Exception as exc:
            if "Authentication required" in str(exc):
                auth_status = "not_authenticated"

        return {
            "location_id": location_id,
            "servings": servings,
            "include_spices_by_default": include_spices_by_default,
            "auth_status": auth_status,
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/servings")
async def set_servings(body: ServingsBody, request: Request):
    """Set the authenticated user's default number of servings per meal."""
    try:
        from kroger_mcp.tools.shared import set_default_servings

        set_default_servings(body.servings, user_id=current_user_id(request))
        return {"success": True, "servings": body.servings}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/include-spices-by-default")
async def set_include_spices(body: IncludeSpicesBody, request: Request):
    """Persist this user's 'Include spices by default' Advanced-Settings toggle."""
    try:
        from kroger_mcp.tools.shared import set_include_spices_by_default

        set_include_spices_by_default(body.include, user_id=current_user_id(request))
        return {"success": True, "include_spices_by_default": body.include}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/location")
async def set_location(body: LocationBody, request: Request):
    """Set the authenticated user's preferred Kroger store location."""
    try:
        from kroger_mcp.tools.shared import set_preferred_location_id

        set_preferred_location_id(body.location_id, user_id=current_user_id(request))
        return {"success": True, "location_id": body.location_id}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/settings/location/search")
async def search_locations(zip: str = Query(..., description="ZIP code to search near")):
    """Search for nearby Kroger stores by ZIP code."""
    try:
        from kroger_mcp.tools.shared import get_client_credentials_client

        client = get_client_credentials_client()
        raw = client.location.search_locations(zip_code=zip, limit=5)

        # Normalise to a flat list of dicts
        locations = []
        items = raw if isinstance(raw, list) else raw.get("data", [])
        for loc in items:
            loc_id = loc.get("locationId") or loc.get("location_id") or ""
            name = loc.get("name", "Kroger")
            # Build a readable address string
            addr_parts = []
            address = loc.get("address", {})
            if address.get("addressLine1"):
                addr_parts.append(address["addressLine1"])
            if address.get("city"):
                addr_parts.append(address["city"])
            if address.get("state"):
                addr_parts.append(address["state"])
            if address.get("zipCode"):
                addr_parts.append(address["zipCode"])
            address_str = ", ".join(addr_parts) if addr_parts else ""

            locations.append(
                {
                    "location_id": loc_id,
                    "name": name,
                    "address": address_str,
                }
            )

        return locations
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


# --------------- Kroger OAuth / Credentials endpoints ---------------


@router.get("/api/settings/auth/status")
async def get_auth_status():
    """Return detailed Kroger auth status and token info."""
    from kroger_mcp.tools.shared import (
        get_authenticated_client,
        get_kroger_credentials,
        get_token_info,
    )

    creds = get_kroger_credentials()
    configured = bool(creds["client_id"] and creds["client_secret"])

    result = {
        "configured": configured,
        "authenticated": False,
        "status": "not_configured",
        "token_info": None,
    }

    if not configured:
        return result

    token_data = get_token_info()
    if token_data:
        result["token_info"] = {
            "scope": token_data.get("scope", ""),
            "token_type": token_data.get("token_type", ""),
            "has_refresh_token": "refresh_token" in token_data,
            "expires_in": token_data.get("expires_in"),
        }

    try:
        get_authenticated_client()
        result["authenticated"] = True
        result["status"] = "authenticated"
    except Exception as exc:
        if "Authentication required" in str(exc):
            result["status"] = "not_authenticated"
        else:
            logger.warning("Auth status check failed: %s", exc)
            result["status"] = "not_configured"
            result["error"] = str(exc)

    return result


@router.post("/api/settings/auth/connect")
async def start_oauth():
    """Start OAuth PKCE flow; returns auth URL for browser redirect."""
    from kroger_api import KrogerAPI
    from kroger_api.utils import generate_pkce_parameters

    from kroger_mcp.tools.shared import get_kroger_credentials

    creds = get_kroger_credentials()
    if not creds["client_id"] or not creds["client_secret"]:
        return JSONResponse(
            status_code=400,
            content={
                "error": "Kroger credentials not configured. Open Advanced Settings to add your Client ID and Secret."
            },
        )

    redirect_uri = creds.get("redirect_uri") or "http://localhost:8000/callback"

    pkce = generate_pkce_parameters()
    state = pkce["code_verifier"][:16]

    try:
        kroger = KrogerAPI(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            redirect_uri=redirect_uri,
        )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    auth_url = kroger.authorization.get_authorization_url(
        scope="product.compact cart.basic:write",
        state=state,
        code_challenge=pkce["code_challenge"],
        code_challenge_method=pkce["code_challenge_method"],
    )

    # Persist PKCE state for the callback
    _WEB_OAUTH_STATE_FILE.write_text(
        json.dumps(
            {
                "pkce_params": pkce,
                "state": state,
                "redirect_uri": redirect_uri,
            }
        )
    )

    return {"auth_url": auth_url}


@router.post("/api/settings/auth/disconnect")
async def disconnect_kroger():
    """Clear Kroger token and disconnect."""
    from kroger_mcp.tools.shared import delete_user_token

    try:
        delete_user_token()
        return {"success": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/settings/credentials")
async def get_credentials(request: Request):
    """Get this user's Kroger API credentials (secret masked)."""
    from kroger_mcp.tools.shared import get_kroger_credentials

    creds = get_kroger_credentials(user_id=current_user_id(request))
    secret = creds["client_secret"]
    return {
        "client_id": creds["client_id"],
        "client_secret_masked": ("*" * 8 + secret[-4:]) if len(secret) > 4 else "",
        "redirect_uri": creds["redirect_uri"],
        "has_secret": bool(secret),
    }


@router.post("/api/settings/credentials")
async def save_credentials(body: CredentialsBody, request: Request):
    """Save this user's Kroger API credentials."""
    from kroger_mcp.tools.shared import (
        invalidate_authenticated_client,
        invalidate_client_credentials_client,
        set_kroger_credentials,
    )

    set_kroger_credentials(
        client_id=body.client_id or None,
        client_secret=body.client_secret or None,
        redirect_uri=body.redirect_uri or None,
        user_id=current_user_id(request),
    )
    invalidate_authenticated_client()
    invalidate_client_credentials_client()
    return {"success": True}
