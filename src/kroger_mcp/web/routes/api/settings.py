"""API routes for settings management."""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


class ServingsBody(BaseModel):
    servings: int


class LocationBody(BaseModel):
    location_id: str


@router.get("/api/settings")
async def get_settings():
    """Return current app settings."""
    try:
        from kroger_mcp.tools.shared import (
            get_preferred_location_id,
            get_default_servings,
            get_authenticated_client,
        )
        location_id = get_preferred_location_id() or ""
        servings = get_default_servings()

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
            "auth_status": auth_status,
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/servings")
async def set_servings(body: ServingsBody):
    """Set the default number of servings per meal."""
    try:
        from kroger_mcp.tools.shared import set_default_servings
        set_default_servings(body.servings)
        return {"success": True, "servings": body.servings}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/settings/location")
async def set_location(body: LocationBody):
    """Set the preferred Kroger store location."""
    try:
        from kroger_mcp.tools.shared import set_preferred_location_id
        set_preferred_location_id(body.location_id)
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

            locations.append({
                "location_id": loc_id,
                "name": name,
                "address": address_str,
            })

        return locations
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
