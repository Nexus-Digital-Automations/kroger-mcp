"""
Reporting and export tools for the Kroger MCP server.
"""

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
            description=(
                "Action: 'get_analytics' - generate analytics report, "
                "'export_data' - export all data for backup, "
                "'check_recipe_pantry' - check pantry inventory for a recipe, "
                "'generate_shopping_list' - create shopping list for multiple recipes, "
                "'get_cookable_recipes' - find recipes you can make with current pantry"
            )
        ),
        report_type: Optional[str] = Field(
            default=None,
            description="Report type: 'spending', 'predictions', 'patterns', 'pantry' (for get_analytics)",
        ),
        days_back: Optional[int] = Field(
            default=30,
            description="Days to analyze (for get_analytics spending/patterns)",
        ),
        include_orders: Optional[bool] = Field(
            default=True,
            description="Include order history (for export_data)",
        ),
        include_products: Optional[bool] = Field(
            default=True,
            description="Include product catalog (for export_data)",
        ),
        include_pantry_data: Optional[bool] = Field(
            default=True,
            description="Include pantry inventory (for export_data)",
        ),
        include_recipes: Optional[bool] = Field(
            default=True,
            description="Include saved recipes (for export_data)",
        ),
        recipe_id: Optional[str] = Field(
            default=None,
            description="Recipe ID (for check_recipe_pantry)",
        ),
        scale: Optional[float] = Field(
            default=1.0,
            description="Recipe scale multiplier (for check_recipe_pantry and generate_shopping_list)",
        ),
        recipe_ids: Optional[List[str]] = Field(
            default=None,
            description="List of recipe IDs (for generate_shopping_list)",
        ),
        skip_in_pantry: Optional[bool] = Field(
            default=True,
            description="Skip items already in pantry (for generate_shopping_list)",
        ),
        pantry_threshold: Optional[int] = Field(
            default=30,
            description="Pantry level % to consider 'have enough' (for generate_shopping_list)",
        ),
        combine_duplicates: Optional[bool] = Field(
            default=True,
            description="Combine same ingredients across recipes (for generate_shopping_list)",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Reporting and analytics operations."""
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
