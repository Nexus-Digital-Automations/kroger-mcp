"""
Location management tools for Kroger MCP server
"""

import asyncio
import functools
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from .shared import (
    get_client_credentials_client,
    get_default_zip_code,
    get_preferred_location_id,
    set_preferred_location_id,
)


def register_tools(mcp):
    """Register location-related tools with the FastMCP server"""

    @mcp.tool()
    async def location(
        action: Literal[
            "search",
            "get_details",
            "set_preferred",
            "get_preferred",
            "check_exists",
            "get_zip",
        ] = Field(
            description="search|get_details|set_preferred|get_preferred|check_exists|get_zip"
        ),
        zip_code: str | None = Field(
            default=None,
            description="Zip code to search near",
        ),
        radius_in_miles: int | None = Field(
            default=10,
            description="Search radius in miles 1-100",
        ),
        limit: int | None = Field(
            default=10,
            description="Number of results 1-200",
        ),
        chain: str | None = Field(
            default=None,
            description="Filter by chain name",
        ),
        location_id: str | None = Field(
            default=None,
            description="Store location ID",
        ),
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Store location management operations."""
        match action:
            case "search":
                if ctx:
                    await ctx.info(
                        f"Searching for Kroger locations near {zip_code or 'default zip code'}"
                    )

                if not zip_code:
                    zip_code = get_default_zip_code()

                client = await asyncio.to_thread(get_client_credentials_client)

                try:
                    locations = await asyncio.to_thread(
                        functools.partial(
                            client.location.search_locations,
                            zip_code=zip_code,
                            radius_in_miles=radius_in_miles or 10,
                            limit=limit or 10,
                            chain=chain,
                        )
                    )

                    if not locations or "data" not in locations or not locations["data"]:
                        return {
                            "success": False,
                            "message": f"No locations found near zip code {zip_code}",
                            "data": [],
                        }

                    formatted_locations = []
                    for loc in locations["data"]:
                        address = loc.get("address", {})
                        formatted_loc = {
                            "location_id": loc.get("locationId"),
                            "name": loc.get("name"),
                            "chain": loc.get("chain"),
                            "phone": loc.get("phone"),
                            "address": {
                                "street": address.get("addressLine1", ""),
                                "city": address.get("city", ""),
                                "state": address.get("state", ""),
                                "zip_code": address.get("zipCode", ""),
                            },
                            "full_address": (
                                f"{address.get('addressLine1', '')}, "
                                f"{address.get('city', '')}, "
                                f"{address.get('state', '')} "
                                f"{address.get('zipCode', '')}"
                            ),
                            "coordinates": loc.get("geolocation", {}),
                            "departments": [
                                dept.get("name") for dept in loc.get("departments", [])
                            ],
                            "department_count": len(loc.get("departments", [])),
                        }

                        if "hours" in loc and "monday" in loc["hours"]:
                            monday = loc["hours"]["monday"]
                            if monday.get("open24", False):
                                formatted_loc["hours_monday"] = "Open 24 hours"
                            elif "open" in monday and "close" in monday:
                                formatted_loc["hours_monday"] = (
                                    f"{monday['open']} - {monday['close']}"
                                )
                            else:
                                formatted_loc["hours_monday"] = "Hours not available"

                        formatted_locations.append(formatted_loc)

                    if ctx:
                        await ctx.info(f"Found {len(formatted_locations)} locations")

                    return {
                        "success": True,
                        "search_params": {
                            "zip_code": zip_code,
                            "radius_miles": radius_in_miles,
                            "limit": limit,
                            "chain": chain,
                        },
                        "count": len(formatted_locations),
                        "data": formatted_locations,
                    }

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error searching locations: {str(e)}")
                    return {"success": False, "error": str(e), "data": []}

            case "get_details":
                if not location_id:
                    return {"success": False, "error": "location_id is required"}
                if ctx:
                    await ctx.info(f"Getting details for location {location_id}")

                client = await asyncio.to_thread(get_client_credentials_client)

                try:
                    location_details = await asyncio.to_thread(
                        client.location.get_location, location_id
                    )

                    if not location_details or "data" not in location_details:
                        return {
                            "success": False,
                            "message": f"Location {location_id} not found",
                        }

                    loc = location_details["data"]

                    departments = []
                    for dept in loc.get("departments", []):
                        dept_info = {
                            "department_id": dept.get("departmentId"),
                            "name": dept.get("name"),
                            "phone": dept.get("phone"),
                        }
                        if "hours" in dept and "monday" in dept["hours"]:
                            monday = dept["hours"]["monday"]
                            if monday.get("open24", False):
                                dept_info["hours_monday"] = "Open 24 hours"
                            elif "open" in monday and "close" in monday:
                                dept_info["hours_monday"] = f"{monday['open']} - {monday['close']}"
                        departments.append(dept_info)

                    address = loc.get("address", {})
                    return {
                        "success": True,
                        "location_id": loc.get("locationId"),
                        "name": loc.get("name"),
                        "chain": loc.get("chain"),
                        "phone": loc.get("phone"),
                        "address": {
                            "street": address.get("addressLine1", ""),
                            "street2": address.get("addressLine2", ""),
                            "city": address.get("city", ""),
                            "state": address.get("state", ""),
                            "zip_code": address.get("zipCode", ""),
                        },
                        "coordinates": loc.get("geolocation", {}),
                        "departments": departments,
                        "department_count": len(departments),
                    }

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error getting location details: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "set_preferred":
                if not location_id:
                    return {"success": False, "error": "location_id is required"}
                if ctx:
                    await ctx.info(f"Setting preferred location to {location_id}")

                client = await asyncio.to_thread(get_client_credentials_client)

                try:
                    exists = await asyncio.to_thread(client.location.location_exists, location_id)
                    if not exists:
                        return {
                            "success": False,
                            "error": f"Location {location_id} does not exist",
                        }

                    location_details = await asyncio.to_thread(
                        client.location.get_location, location_id
                    )
                    loc_data = location_details.get("data", {})

                    set_preferred_location_id(location_id)

                    if ctx:
                        await ctx.info(
                            f"Preferred location set to {loc_data.get('name', location_id)}"
                        )

                    return {
                        "success": True,
                        "preferred_location_id": location_id,
                        "location_name": loc_data.get("name"),
                        "message": f"Preferred location set to {loc_data.get('name', location_id)}",
                    }

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error setting preferred location: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "get_preferred":
                preferred_location_id = get_preferred_location_id()

                if not preferred_location_id:
                    return {
                        "success": False,
                        "message": "No preferred location set. Use location(action='set_preferred') to set one.",
                    }

                if ctx:
                    await ctx.info(
                        f"Getting preferred location details for {preferred_location_id}"
                    )

                client = await asyncio.to_thread(get_client_credentials_client)

                try:
                    location_details = await asyncio.to_thread(
                        client.location.get_location, preferred_location_id
                    )
                    loc_data = location_details.get("data", {})

                    return {
                        "success": True,
                        "preferred_location_id": preferred_location_id,
                        "location_details": {
                            "name": loc_data.get("name"),
                            "chain": loc_data.get("chain"),
                            "phone": loc_data.get("phone"),
                            "address": loc_data.get("address", {}),
                        },
                    }

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error getting preferred location details: {str(e)}")
                    return {
                        "success": False,
                        "error": str(e),
                        "preferred_location_id": preferred_location_id,
                    }

            case "check_exists":
                if not location_id:
                    return {"success": False, "error": "location_id is required"}
                if ctx:
                    await ctx.info(f"Checking if location {location_id} exists")

                client = await asyncio.to_thread(get_client_credentials_client)

                try:
                    exists = await asyncio.to_thread(client.location.location_exists, location_id)
                    return {
                        "success": True,
                        "location_id": location_id,
                        "exists": exists,
                        "message": f"Location {location_id} {'exists' if exists else 'does not exist'}",
                    }

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error checking location existence: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "get_zip":
                zip_code_val = get_default_zip_code()
                return {"user_zip_code": zip_code_val}

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
