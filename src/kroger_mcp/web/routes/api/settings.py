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


class FavoritesDisplayModeBody(BaseModel):
    mode: str


class MealPlanDeductionModeBody(BaseModel):
    mode: str


class PlanningSettingBody(BaseModel):
    value: int


class LocationBody(BaseModel):
    location_id: str


class CredentialsBody(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""


class ConsentBody(BaseModel):
    # Maps consent category keys to opt-in booleans; omitted keys keep their value.
    updates: dict[str, bool] = {}


@router.get("/api/settings")
async def get_settings(request: Request):
    """Return current app settings for the authenticated user."""
    try:
        from kroger_mcp.tools.shared import (
            get_authenticated_client,
            get_default_servings,
            get_draft_dinners_per_week,
            get_favorites_display_mode,
            get_include_spices_by_default,
            get_meal_plan_pantry_deduction_mode,
            get_planning_horizon_days,
            get_preferred_location_id,
            get_week_start_day,
            should_show_deduction_default_notice,
        )

        user_id = current_user_id(request)
        location_id = get_preferred_location_id(user_id=user_id) or ""
        servings = get_default_servings(user_id=user_id)
        include_spices_by_default = get_include_spices_by_default(user_id=user_id)
        favorites_display_mode = get_favorites_display_mode(user_id=user_id)
        meal_plan_pantry_deduction_mode = get_meal_plan_pantry_deduction_mode(user_id=user_id)
        show_meal_plan_deduction_notice = should_show_deduction_default_notice(user_id=user_id)

        auth_status = "not_configured"
        try:
            get_authenticated_client(user_id)
            auth_status = "authenticated"
        except Exception as exc:
            if "Authentication required" in str(exc):
                auth_status = "not_authenticated"

        return {
            "location_id": location_id,
            "servings": servings,
            "include_spices_by_default": include_spices_by_default,
            "favorites_display_mode": favorites_display_mode,
            "meal_plan_pantry_deduction_mode": meal_plan_pantry_deduction_mode,
            "show_meal_plan_deduction_notice": show_meal_plan_deduction_notice,
            "week_start_day": get_week_start_day(user_id=user_id),
            "planning_horizon_days": get_planning_horizon_days(user_id=user_id),
            "draft_dinners_per_week": get_draft_dinners_per_week(user_id=user_id),
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


@router.post("/api/settings/favorites-display-mode")
async def set_favorites_display_mode_route(body: FavoritesDisplayModeBody, request: Request):
    """Persist this user's favorites-on-sale display mode ('sort' or 'section')."""
    try:
        from kroger_mcp.tools.shared import set_favorites_display_mode

        set_favorites_display_mode(body.mode, user_id=current_user_id(request))
        return {"success": True, "favorites_display_mode": body.mode}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/meal-plan-pantry-deduction-mode")
async def set_meal_plan_pantry_deduction_mode_route(
    body: MealPlanDeductionModeBody, request: Request
):
    """Persist this user's meal-plan pantry deduction mode ('automatic' or 'confirm')."""
    try:
        from kroger_mcp.tools.shared import set_meal_plan_pantry_deduction_mode

        set_meal_plan_pantry_deduction_mode(body.mode, user_id=current_user_id(request))
        return {"success": True, "meal_plan_pantry_deduction_mode": body.mode}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/week-start-day")
async def set_week_start_day_route(body: PlanningSettingBody, request: Request):
    """Persist this user's week start day (0=Monday .. 6=Sunday)."""
    try:
        from kroger_mcp.tools.shared import set_week_start_day

        set_week_start_day(body.value, user_id=current_user_id(request))
        return {"success": True, "week_start_day": body.value}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/planning-horizon-days")
async def set_planning_horizon_days_route(body: PlanningSettingBody, request: Request):
    """Persist this user's planning horizon in days (1-28)."""
    try:
        from kroger_mcp.tools.shared import set_planning_horizon_days

        set_planning_horizon_days(body.value, user_id=current_user_id(request))
        return {"success": True, "planning_horizon_days": body.value}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/draft-dinners-per-week")
async def set_draft_dinners_per_week_route(body: PlanningSettingBody, request: Request):
    """Persist how many dinners the weekly auto-draft fills (1-7)."""
    try:
        from kroger_mcp.tools.shared import set_draft_dinners_per_week

        set_draft_dinners_per_week(body.value, user_id=current_user_id(request))
        return {"success": True, "draft_dinners_per_week": body.value}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/meal-plan-deduction-notice-seen")
async def dismiss_meal_plan_deduction_notice(request: Request):
    """Mark the one-time "pantry now auto-deducts by default" toast as seen."""
    try:
        from kroger_mcp.tools.shared import mark_deduction_default_notice_seen

        mark_deduction_default_notice_seen(user_id=current_user_id(request))
        return {"success": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/settings/consent")
async def get_consent_state(request: Request):
    """Return this user's data-sharing consent state (all categories off by default)."""
    try:
        from kroger_mcp.analytics import consent

        return consent.get_consent(user_id=current_user_id(request))
    except Exception as exc:
        logger.error("get_consent failed: %s", exc, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/consent")
async def set_consent_state(body: ConsentBody, request: Request):
    """Apply per-category opt-in choices and mark consent as decided."""
    try:
        from kroger_mcp.analytics import consent

        return consent.set_consent(body.updates, user_id=current_user_id(request))
    except KeyError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        logger.error("set_consent failed: %s", exc, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/consent/withdraw")
async def withdraw_consent_state(request: Request):
    """Disable every sharing category while keeping the decision on record."""
    try:
        from kroger_mcp.analytics import consent

        return consent.withdraw_consent(user_id=current_user_id(request))
    except Exception as exc:
        logger.error("withdraw_consent failed: %s", exc, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/settings/consent/data")
async def delete_shared_consent_data(request: Request):
    """Withdraw consent and purge any shared-derived data for this user."""
    try:
        from kroger_mcp.analytics import consent

        return consent.delete_shared_data(user_id=current_user_id(request))
    except Exception as exc:
        logger.error("delete_shared_data failed: %s", exc, exc_info=True)
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
async def search_locations(
    request: Request, zip: str = Query(..., description="ZIP code to search near")
):
    """Search for nearby Kroger stores by ZIP code."""
    try:
        from kroger_mcp.cache import cache_read_through
        from kroger_mcp.tools.shared import get_client_credentials_client, kroger_cache_key

        client = get_client_credentials_client(current_user_id(request))
        # Store locations are stable; share a 6h cache across users (keyed by
        # client_id + zip) so repeat ZIP lookups don't each hit Kroger.
        raw = cache_read_through(
            kroger_cache_key(client, "location_search", zip=zip, limit=5),
            21600,
            lambda: client.location.search_locations(zip_code=zip, limit=5),
        )

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
async def get_auth_status(request: Request):
    """Return detailed Kroger auth status and token info."""
    from kroger_mcp.auth.kroger_tokens import load_kroger_token
    from kroger_mcp.tools.shared import (
        get_authenticated_client,
        get_kroger_credentials,
    )

    creds = get_kroger_credentials(user_id=current_user_id(request))
    configured = bool(creds["client_id"] and creds["client_secret"])

    result = {
        "configured": configured,
        "authenticated": False,
        "status": "not_configured",
        "token_info": None,
    }

    if not configured:
        return result

    # Per-user token metadata (was the shared-file get_token_info(), which leaked
    # one user's token details into every user's auth-status view).
    token_data = load_kroger_token(current_user_id(request))
    if token_data:
        result["token_info"] = {
            "scope": token_data.get("scope", ""),
            "token_type": token_data.get("token_type", ""),
            "has_refresh_token": "refresh_token" in token_data,
            "expires_in": token_data.get("expires_in"),
        }

    try:
        get_authenticated_client(current_user_id(request))
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
async def start_oauth(request: Request):
    """Start OAuth PKCE flow; returns auth URL for browser redirect."""
    from kroger_api import KrogerAPI
    from kroger_api.utils import generate_pkce_parameters

    from kroger_mcp.tools.shared import get_kroger_credentials

    creds = get_kroger_credentials(user_id=current_user_id(request))
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

    from kroger_mcp.tools.shared import KROGER_OAUTH_SCOPES

    auth_url = kroger.authorization.get_authorization_url(
        scope=KROGER_OAUTH_SCOPES,
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
async def disconnect_kroger(request: Request):
    """Clear this user's Kroger token and disconnect."""
    from kroger_mcp.tools.shared import invalidate_authenticated_client

    try:
        invalidate_authenticated_client(user_id=current_user_id(request))
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
    """Save this user's Kroger API credentials.

    Evicts the caller's cached app client so the new secret takes effect. When
    the ``client_id`` itself changes, the user's stored OAuth token was minted
    under a different app and is no longer valid — drop it so they re-link.
    """
    from kroger_mcp.tools.shared import (
        get_kroger_credentials,
        invalidate_authenticated_client,
        invalidate_client_credentials_client,
        set_kroger_credentials,
    )

    user_id = current_user_id(request)
    old_client_id = get_kroger_credentials(user_id=user_id)["client_id"]

    set_kroger_credentials(
        client_id=body.client_id or None,
        client_secret=body.client_secret or None,
        redirect_uri=body.redirect_uri or None,
        user_id=user_id,
    )

    new_client_id = get_kroger_credentials(user_id=user_id)["client_id"]
    invalidate_client_credentials_client(user_id)
    if new_client_id != old_client_id:
        invalidate_authenticated_client(user_id)
    return {"success": True}
