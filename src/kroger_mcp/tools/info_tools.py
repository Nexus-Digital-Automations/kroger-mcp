"""
Chain, department, and utility information tools for Kroger MCP server.
"""

import asyncio
from datetime import datetime
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from kroger_mcp.cache import cache_read_through

from ..auth.dependencies import mcp_user_id
from .shared import get_client_credentials_client, kroger_cache_key

# Chains and departments are effectively static reference data — cache 24h.
_REFERENCE_TTL = 86400


def register_tools(mcp):
    """Register info-related tools with the FastMCP server."""

    @mcp.tool()
    async def info(
        action: Literal[
            "list_chains",
            "get_chain",
            "check_chain",
            "list_departments",
            "get_department",
            "check_department",
            "get_datetime",
            "get_servings",
            "set_servings",
            "get_preferences",
            "set_week_start_day",
            "set_planning_horizon_days",
            "set_draft_dinners_per_week",
        ] = Field(
            description=(
                "set_servings — set household size (used by recipe scaling). "
                "get_preferences — current location + servings + planning settings. "
                "set_week_start_day — value 0-6 (0=Monday..6=Sunday, default 6). "
                "set_planning_horizon_days — value 1-28, days a plan covers (default 7). "
                "set_draft_dinners_per_week — value 1-7, dinners the weekly auto-draft "
                "fills (default 3). "
                "Other: list_chains|get_chain|check_chain|list_departments|get_department|check_department|get_datetime|get_servings"
            )
        ),
        chain_name: str | None = Field(
            default=None,
            description="Chain name",
        ),
        department_id: str | None = Field(
            default=None,
            description="Department ID",
        ),
        servings: int | None = Field(
            default=None,
            description="Number of servings 1-20",
        ),
        value: int | None = Field(
            default=None,
            description="New value for the set_week_start_day / "
            "set_planning_horizon_days / set_draft_dinners_per_week actions",
        ),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Store info and user preferences.

        Store data: list_chains, get_chain, check_chain, list_departments, get_department, check_department.
        Preferences: set_servings (household size, affects recipe scaling), get_servings, get_preferences.
        Utility: get_datetime.
        """
        user_id = mcp_user_id()
        return await asyncio.to_thread(
            _info_impl,
            action,
            chain_name,
            department_id,
            servings,
            value,
            ctx,
            user_id,
        )

    def _info_impl(action, chain_name, department_id, servings, value, ctx, user_id):
        match action:
            case "list_chains":
                if ctx:
                    ctx.info("Getting list of Kroger chains")

                client = get_client_credentials_client(user_id)

                try:
                    chains = cache_read_through(
                        kroger_cache_key(client, "chains"),
                        _REFERENCE_TTL,
                        client.location.list_chains,
                    )

                    if not chains or "data" not in chains or not chains["data"]:
                        return {"success": False, "message": "No chains found", "data": []}

                    formatted_chains = [
                        {
                            "name": chain.get("name"),
                            "division_numbers": chain.get("divisionNumbers", []),
                        }
                        for chain in chains["data"]
                    ]

                    if ctx:
                        ctx.info(f"Found {len(formatted_chains)} chains")

                    return {
                        "success": True,
                        "count": len(formatted_chains),
                        "data": formatted_chains,
                    }

                except Exception as e:
                    if ctx:
                        ctx.error(f"Error listing chains: {str(e)}")
                    return {"success": False, "error": str(e), "data": []}

            case "get_chain":
                if not chain_name:
                    return {"success": False, "error": "chain_name is required"}

                if ctx:
                    ctx.info(f"Getting details for chain: {chain_name}")

                client = get_client_credentials_client(user_id)

                try:
                    chain_details = client.location.get_chain(chain_name)

                    if not chain_details or "data" not in chain_details:
                        return {"success": False, "message": f"Chain '{chain_name}' not found"}

                    chain = chain_details["data"]

                    return {
                        "success": True,
                        "name": chain.get("name"),
                        "division_numbers": chain.get("divisionNumbers", []),
                    }

                except Exception as e:
                    if ctx:
                        ctx.error(f"Error getting chain details: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "check_chain":
                if not chain_name:
                    return {"success": False, "error": "chain_name is required"}

                if ctx:
                    ctx.info(f"Checking if chain '{chain_name}' exists")

                client = get_client_credentials_client(user_id)

                try:
                    exists = client.location.chain_exists(chain_name)

                    return {
                        "success": True,
                        "chain_name": chain_name,
                        "exists": exists,
                        "message": f"Chain '{chain_name}' {'exists' if exists else 'does not exist'}",
                    }

                except Exception as e:
                    if ctx:
                        ctx.error(f"Error checking chain existence: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "list_departments":
                if ctx:
                    ctx.info("Getting list of departments")

                client = get_client_credentials_client(user_id)

                try:
                    departments = cache_read_through(
                        kroger_cache_key(client, "departments"),
                        _REFERENCE_TTL,
                        client.location.list_departments,
                    )

                    if not departments or "data" not in departments or not departments["data"]:
                        return {"success": False, "message": "No departments found", "data": []}

                    formatted_departments = [
                        {
                            "department_id": dept.get("departmentId"),
                            "name": dept.get("name"),
                        }
                        for dept in departments["data"]
                    ]

                    if ctx:
                        ctx.info(f"Found {len(formatted_departments)} departments")

                    return {
                        "success": True,
                        "count": len(formatted_departments),
                        "data": formatted_departments,
                    }

                except Exception as e:
                    if ctx:
                        ctx.error(f"Error listing departments: {str(e)}")
                    return {"success": False, "error": str(e), "data": []}

            case "get_department":
                if not department_id:
                    return {"success": False, "error": "department_id is required"}

                if ctx:
                    ctx.info(f"Getting details for department: {department_id}")

                client = get_client_credentials_client(user_id)

                try:
                    dept_details = client.location.get_department(department_id)

                    if not dept_details or "data" not in dept_details:
                        return {
                            "success": False,
                            "message": f"Department '{department_id}' not found",
                        }

                    dept = dept_details["data"]

                    return {
                        "success": True,
                        "department_id": dept.get("departmentId"),
                        "name": dept.get("name"),
                    }

                except Exception as e:
                    if ctx:
                        ctx.error(f"Error getting department details: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "check_department":
                if not department_id:
                    return {"success": False, "error": "department_id is required"}

                if ctx:
                    ctx.info(f"Checking if department '{department_id}' exists")

                client = get_client_credentials_client(user_id)

                try:
                    exists = client.location.department_exists(department_id)

                    return {
                        "success": True,
                        "department_id": department_id,
                        "exists": exists,
                        "message": (
                            f"Department '{department_id}' "
                            f"{'exists' if exists else 'does not exist'}"
                        ),
                    }

                except Exception as e:
                    if ctx:
                        ctx.error(f"Error checking department existence: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "get_datetime":
                now = datetime.now()

                return {
                    "success": True,
                    "datetime": now.isoformat(),
                    "date": now.date().isoformat(),
                    "time": now.time().isoformat(),
                    "timestamp": int(now.timestamp()),
                    "formatted": now.strftime("%A, %B %d, %Y at %I:%M:%S %p"),
                }

            case "get_servings":
                try:
                    from .shared import get_default_servings as _get_default_servings

                    svc = _get_default_servings(user_id)
                    return {
                        "success": True,
                        "default_servings": svc,
                        "description": f"Recipes will default to {svc} serving(s)",
                        "usage": {
                            "recipe_creation": f"New recipes default to {svc} servings",
                            "shopping_list": f"Shopping list scales to {svc} servings",
                            "meal_planning": f"Meal assignments default to {svc} servings",
                            "can_override": "You can override this per-recipe or per-meal",
                        },
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get default servings: {str(e)}"}

            case "set_servings":
                if servings is None:
                    return {"success": False, "error": "servings is required for set_servings"}
                if not 1 <= servings <= 20:
                    return {"success": False, "error": "servings must be between 1 and 20"}
                try:
                    from .shared import (
                        get_default_servings as _get_default_servings,
                    )
                    from .shared import (
                        set_default_servings as _set_default_servings,
                    )

                    old = _get_default_servings(user_id)
                    _set_default_servings(servings, user_id)
                    return {
                        "success": True,
                        "default_servings": servings,
                        "previous_value": old,
                        "message": f"Default servings updated from {old} to {servings}",
                        "note": (
                            "This will affect new recipes and shopping list scaling. "
                            "Existing recipes retain their servings."
                        ),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to set default servings: {str(e)}"}

            case "get_preferences":
                try:
                    from .shared import get_default_servings as _get_default_servings
                    from .shared import (
                        get_draft_dinners_per_week,
                        get_planning_horizon_days,
                        get_preferred_location_id,
                        get_week_start_day,
                    )

                    return {
                        "success": True,
                        "profile": {
                            "preferred_location_id": get_preferred_location_id(user_id),
                            "default_servings_per_meal": _get_default_servings(user_id),
                            "week_start_day": get_week_start_day(user_id=user_id),
                            "planning_horizon_days": get_planning_horizon_days(user_id=user_id),
                            "draft_dinners_per_week": get_draft_dinners_per_week(user_id=user_id),
                        },
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get preferences: {str(e)}"}

            case "set_week_start_day" | "set_planning_horizon_days" | "set_draft_dinners_per_week":
                if value is None:
                    return {"success": False, "error": f"value is required for {action}"}
                from .shared import (
                    set_draft_dinners_per_week,
                    set_planning_horizon_days,
                    set_week_start_day,
                )

                setters = {
                    "set_week_start_day": set_week_start_day,
                    "set_planning_horizon_days": set_planning_horizon_days,
                    "set_draft_dinners_per_week": set_draft_dinners_per_week,
                }
                setting_name = action.removeprefix("set_")
                try:
                    setters[action](value, user_id=user_id)
                except ValueError as e:
                    return {"success": False, "error": str(e)}
                return {
                    "success": True,
                    setting_name: value,
                    "message": f"{setting_name} set to {value}",
                }

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
