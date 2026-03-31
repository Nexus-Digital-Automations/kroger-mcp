"""
Reporting and export tools for the Kroger MCP server.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastmcp import Context
from pydantic import Field


def register_tools(mcp):
    """Register reporting and export tools with the FastMCP server."""

    @mcp.tool()
    async def reports(
        action: Literal[
            "get_analytics",
            "export_data",
            "check_recipe_pantry",
            "generate_shopping_list",
            "get_cookable_recipes",
        ] = Field(
            description="get_analytics|export_data|check_recipe_pantry|generate_shopping_list|get_cookable_recipes"
        ),
        report_type: Optional[str] = Field(
            default=None,
            description="spending|predictions|patterns|pantry",
        ),
        days_back: Optional[int] = Field(
            default=30,
            description="Days to analyze",
        ),
        include_orders: Optional[bool] = Field(
            default=True,
            description="Include order history",
        ),
        include_products: Optional[bool] = Field(
            default=True,
            description="Include product catalog",
        ),
        include_pantry_data: Optional[bool] = Field(
            default=True,
            description="Include pantry inventory",
        ),
        include_recipes: Optional[bool] = Field(
            default=True,
            description="Include saved recipes",
        ),
        recipe_id: Optional[str] = Field(
            default=None,
            description="Recipe ID",
        ),
        scale: Optional[float] = Field(
            default=1.0,
            description="Recipe scale multiplier",
        ),
        recipe_ids: Optional[List[str]] = Field(
            default=None,
            description="List of recipe IDs",
        ),
        skip_in_pantry: Optional[bool] = Field(
            default=True,
            description="Skip items already in pantry",
        ),
        pantry_threshold: Optional[int] = Field(
            default=30,
            description="Pantry level % threshold",
        ),
        combine_duplicates: Optional[bool] = Field(
            default=True,
            description="Combine same ingredients across recipes",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Reporting and analytics operations."""
        return await asyncio.to_thread(
            _reports_impl, action, report_type, days_back, include_orders,
            include_products, include_pantry_data, include_recipes, recipe_id,
            scale, recipe_ids, skip_in_pantry, pantry_threshold, combine_duplicates,
            ctx,
        )

    def _reports_impl(
        action, report_type, days_back, include_orders,
        include_products, include_pantry_data, include_recipes, recipe_id,
        scale, recipe_ids, skip_in_pantry, pantry_threshold, combine_duplicates,
        ctx,
    ):
        match action:
            case "get_analytics":
                try:
                    if report_type == "spending":
                        from ..analytics.reporting import generate_spending_report

                        report = generate_spending_report(days_back=days_back or 30)
                    elif report_type == "predictions":
                        from ..analytics.reporting import (
                            generate_prediction_accuracy_report,
                        )

                        report = generate_prediction_accuracy_report()
                    elif report_type == "patterns":
                        from ..analytics.reporting import generate_patterns_report

                        report = generate_patterns_report(days_back=days_back or 30)
                    elif report_type == "pantry":
                        from ..analytics.reporting import generate_pantry_report

                        report = generate_pantry_report()
                    else:
                        return {
                            "success": False,
                            "error": (
                                f"Unknown report type: {report_type}. "
                                "Use 'spending', 'predictions', 'patterns', or 'pantry'"
                            ),
                        }
                    return {
                        "success": True,
                        "report_type": report_type,
                        "generated_at": datetime.now().isoformat(),
                        "data": report,
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to generate report: {str(e)}",
                    }

            case "export_data":
                try:
                    from ..analytics.reporting import export_all_data

                    export = export_all_data(
                        include_orders=include_orders if include_orders is not None else True,
                        include_products=include_products if include_products is not None else True,
                        include_pantry=include_pantry_data if include_pantry_data is not None else True,
                        include_recipes=include_recipes if include_recipes is not None else True,
                    )
                    return {"success": True, "export": export}
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to export data: {str(e)}",
                    }

            case "check_recipe_pantry":
                if not recipe_id:
                    return {"success": False, "error": "recipe_id is required"}
                try:
                    from ..analytics.recipe_integration import check_recipe_pantry

                    result = check_recipe_pantry(recipe_id, scale=scale or 1.0)
                    return result
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to check recipe: {str(e)}",
                    }

            case "generate_shopping_list":
                if not recipe_ids:
                    return {"success": False, "error": "recipe_ids is required"}
                try:
                    from ..analytics.recipe_integration import generate_shopping_list

                    result = generate_shopping_list(
                        recipe_ids=recipe_ids,
                        combine_duplicates=combine_duplicates if combine_duplicates is not None else True,
                        skip_in_pantry=skip_in_pantry if skip_in_pantry is not None else True,
                        pantry_threshold=pantry_threshold if pantry_threshold is not None else 30,
                        scale=scale or 1.0,
                    )
                    return result
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to generate shopping list: {str(e)}",
                    }

            case "get_cookable_recipes":
                try:
                    from ..analytics.recipe_integration import get_recipes_for_pantry

                    result = get_recipes_for_pantry()
                    return {"success": True, **result}
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to get cookable recipes: {str(e)}",
                    }

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
