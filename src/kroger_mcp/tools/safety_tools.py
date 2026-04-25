"""
Safety management tools for Kroger MCP server.
"""

import asyncio
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from ..analytics import safety as _safety
from ..analytics.ingredients import (
    get_all_ingredients,
)


def register_tools(mcp):
    """Register safety-related tools with the FastMCP server."""

    @mcp.tool()
    async def safety(
        action: Literal[
            "get_settings",
            "configure",
            "get_bad_ingredients",
            "toggle_ingredient",
            "get_preferences",
            "reset_preferences",
            "approve_product",
            "unapprove_product",
            "get_safe_products",
            "block_product",
            "unblock_product",
            "get_blocked_products",
            "check_product",
            "check_products",
            "check_cart",
        ] = Field(
            description="get_settings|configure|get_bad_ingredients|toggle_ingredient|get_preferences|reset_preferences|approve_product|unapprove_product|get_safe_products|block_product|unblock_product|get_blocked_products|check_product|check_products|check_cart"
        ),
        filtering_enabled: bool | None = Field(
            default=None,
            description="Enable or disable ingredient filtering",
        ),
        block_mode: str | None = Field(
            default=None,
            description="soft|hard|warn_only",
        ),
        include_custom: bool | None = Field(
            default=True,
            description="Include custom ingredients",
        ),
        include_overrides: bool | None = Field(
            default=True,
            description="Apply user overrides",
        ),
        filter_severity: Literal["critical", "warning", "watch"] | None = Field(
            default=None,
            description="Filter by severity",
        ),
        filter_category: str | None = Field(
            default=None,
            description="Filter by category",
        ),
        ingredient_key: str | None = Field(
            default=None,
            description="Ingredient key e.g. msg",
        ),
        enabled: bool | None = Field(
            default=None,
            description="True=enable, False=disable",
        ),
        product_id: str | None = Field(
            default=None,
            description="Kroger product ID",
        ),
        description: str | None = Field(
            default=None,
            description="Product description",
        ),
        brand: str | None = Field(
            default=None,
            description="Product brand",
        ),
        reason: str | None = Field(
            default=None,
            description="Reason for approval or blocking",
        ),
        products: list[dict[str, Any]] | None = Field(
            default=None,
            description="List of {product_id, description}",
        ),
        product_ids: list[str] | None = Field(
            default=None,
            description="Batch unapprove/unblock: list of product IDs",
        ),
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Ingredient safety filter and product management operations."""
        return await asyncio.to_thread(
            _safety_impl,
            action,
            filtering_enabled,
            block_mode,
            include_custom,
            include_overrides,
            filter_severity,
            filter_category,
            ingredient_key,
            enabled,
            product_id,
            description,
            brand,
            reason,
            products,
            product_ids,
            ctx,
        )

    def _safety_impl(
        action,
        filtering_enabled,
        block_mode,
        include_custom,
        include_overrides,
        filter_severity,
        filter_category,
        ingredient_key,
        enabled,
        product_id,
        description,
        brand,
        reason,
        products,
        product_ids,
        ctx,
    ):
        match action:
            case "get_settings":
                if ctx:
                    ctx.info("Getting safety filter settings")

                settings = _safety.get_safety_settings()
                return {
                    "success": True,
                    **settings,
                    "block_mode_options": {
                        "soft": "Warn but allow with confirmation",
                        "hard": "Hide from search, block cart additions",
                        "warn_only": "Show warnings only, no blocking",
                    },
                }

            case "configure":
                if ctx:
                    ctx.info("Updating safety settings")

                try:
                    settings = _safety.update_safety_settings(
                        filtering_enabled=filtering_enabled,
                        block_mode=block_mode,
                    )
                    return {"success": True, **settings}
                except ValueError as e:
                    return {"success": False, "error": str(e)}

            case "get_bad_ingredients":
                if ctx:
                    ctx.info("Getting bad ingredients list")

                from ..analytics.ingredients import get_active_ingredients

                inc_custom = include_custom if include_custom is not None else True
                inc_overrides = include_overrides if include_overrides is not None else True

                if inc_custom and inc_overrides:
                    ing_list = get_active_ingredients(include_custom=True)
                elif inc_overrides:
                    ing_list = get_active_ingredients(include_custom=False)
                elif inc_custom:
                    ing_list = get_active_ingredients(include_custom=True)
                else:
                    ing_list = get_all_ingredients()

                if filter_severity:
                    ing_list = [i for i in ing_list if i["severity"] == filter_severity]

                if filter_category:
                    ing_list = [i for i in ing_list if i.get("category") == filter_category]

                by_severity = {
                    "critical": [i for i in ing_list if i["severity"] == "critical"],
                    "warning": [i for i in ing_list if i["severity"] == "warning"],
                    "watch": [i for i in ing_list if i["severity"] == "watch"],
                }

                system_count = len([i for i in ing_list if i.get("source") == "system"])
                custom_count = len([i for i in ing_list if i.get("source") == "custom"])

                return {
                    "success": True,
                    "total_ingredients": len(ing_list),
                    "system_ingredients": system_count,
                    "custom_ingredients": custom_count,
                    "by_severity": {
                        "critical": len(by_severity["critical"]),
                        "warning": len(by_severity["warning"]),
                        "watch": len(by_severity["watch"]),
                    },
                    "categories": sorted(
                        set(i.get("category", "") for i in ing_list if i.get("category"))
                    ),
                    "severity_levels": ["critical", "warning", "watch"],
                    "ingredients": ing_list,
                }

            case "toggle_ingredient":
                if not ingredient_key:
                    return {"success": False, "error": "ingredient_key is required"}
                if enabled is None:
                    return {"success": False, "error": "enabled is required"}
                if ctx:
                    action_word = "Enabling" if enabled else "Disabling"
                    ctx.info(f"{action_word} ingredient check: {ingredient_key}")

                return _safety.toggle_ingredient(ingredient_key, enabled)

            case "get_preferences":
                if ctx:
                    ctx.info("Getting ingredient preferences")

                prefs = _safety.get_ingredient_preferences()
                disabled = [p for p in prefs if not p.get("enabled", True)]

                return {
                    "success": True,
                    "total_preferences": len(prefs),
                    "disabled_count": len(disabled),
                    "preferences": prefs,
                    "note": "Ingredients not listed are enabled by default",
                }

            case "reset_preferences":
                if ctx:
                    ctx.info("Resetting ingredient preferences to defaults")

                return _safety.reset_ingredient_preferences()

            case "approve_product":
                if products:
                    if ctx:
                        ctx.info(f"Batch approving {len(products)} products")
                    results = [
                        _safety.add_to_safe_list(
                            product_id=p["product_id"],
                            description=p.get("description"),
                            brand=p.get("brand"),
                            reason=p.get("reason"),
                        )
                        for p in products
                    ]
                    approved = sum(1 for r in results if r.get("success"))
                    return {
                        "success": True,
                        "approved": approved,
                        "total": len(products),
                        "results": results,
                    }

                if not product_id:
                    return {"success": False, "error": "product_id or products is required"}
                if ctx:
                    ctx.info(f"Approving product {product_id}")

                return _safety.add_to_safe_list(
                    product_id=product_id,
                    description=description,
                    brand=brand,
                    reason=reason,
                )

            case "unapprove_product":
                ids = product_ids if product_ids else ([product_id] if product_id else None)
                if not ids:
                    return {"success": False, "error": "product_id or product_ids is required"}
                if ctx:
                    ctx.info(f"Removing {len(ids)} product(s) from safe list")

                if len(ids) == 1:
                    return _safety.remove_from_safe_list(ids[0])

                results = {pid: _safety.remove_from_safe_list(pid) for pid in ids}
                removed = sum(1 for r in results.values() if r.get("success"))
                return {"success": True, "removed": removed, "total": len(ids), "results": results}

            case "get_safe_products":
                if ctx:
                    ctx.info("Getting safe products list")

                safe_products = _safety.get_safe_products()
                return {
                    "success": True,
                    "count": len(safe_products),
                    "products": safe_products,
                }

            case "block_product":
                if products:
                    if ctx:
                        ctx.info(f"Batch blocking {len(products)} products")
                    results = [
                        _safety.add_to_blocked_list(
                            product_id=p["product_id"],
                            description=p.get("description"),
                            reason=p.get("reason"),
                        )
                        for p in products
                    ]
                    blocked = sum(1 for r in results if r.get("success"))
                    return {
                        "success": True,
                        "blocked": blocked,
                        "total": len(products),
                        "results": results,
                    }

                if not product_id:
                    return {"success": False, "error": "product_id or products is required"}
                if ctx:
                    ctx.info(f"Blocking product {product_id}")

                return _safety.add_to_blocked_list(
                    product_id=product_id,
                    description=description,
                    reason=reason,
                )

            case "unblock_product":
                ids = product_ids if product_ids else ([product_id] if product_id else None)
                if not ids:
                    return {"success": False, "error": "product_id or product_ids is required"}
                if ctx:
                    ctx.info(f"Unblocking {len(ids)} product(s)")

                if len(ids) == 1:
                    return _safety.remove_from_blocked_list(ids[0])

                results = {pid: _safety.remove_from_blocked_list(pid) for pid in ids}
                unblocked = sum(1 for r in results.values() if r.get("success"))
                return {
                    "success": True,
                    "unblocked": unblocked,
                    "total": len(ids),
                    "results": results,
                }

            case "get_blocked_products":
                if ctx:
                    ctx.info("Getting blocked products list")

                blocked_products = _safety.get_blocked_products()
                return {
                    "success": True,
                    "count": len(blocked_products),
                    "products": blocked_products,
                }

            case "check_product":
                if not product_id:
                    return {"success": False, "error": "product_id is required"}
                if not description:
                    return {"success": False, "error": "description is required"}
                if ctx:
                    ctx.info(f"Checking safety for product {product_id}")

                status = _safety.get_product_safety_status(
                    product_id=product_id,
                    description=description,
                    brand=brand,
                )
                return {"success": True, **status.to_dict()}

            case "check_products":
                if not products:
                    return {"success": False, "error": "products list is required"}
                if ctx:
                    ctx.info(f"Checking safety for {len(products)} products")

                if len(products) > 50:
                    return {
                        "success": False,
                        "error": "Maximum 50 products per request",
                    }

                statuses = _safety.check_products_safety_batch(products)
                return {
                    "success": True,
                    "count": len(statuses),
                    "results": [s.to_dict() for s in statuses],
                }

            case "check_cart":
                if ctx:
                    ctx.info("Scanning cart for safety concerns")

                from .cart_tools import _load_cart_data

                try:
                    cart_data = _load_cart_data()
                    cart_items = cart_data.get("current_cart", [])
                except Exception:
                    cart_items = []

                if not cart_items:
                    return {
                        "success": True,
                        "message": "Cart is empty",
                        "safe_items": [],
                        "flagged_items": [],
                        "blocked_items": [],
                    }

                prods = [
                    {
                        "product_id": item.get("product_id", ""),
                        "description": item.get("description", ""),
                        "brand": item.get("brand"),
                    }
                    for item in cart_items
                ]

                statuses = _safety.check_products_safety_batch(prods)

                safe_items = []
                flagged_items = []
                blocked_items = []

                for i, status in enumerate(statuses):
                    item_info = {**prods[i], **status.to_dict()}

                    if status.is_blocked:
                        blocked_items.append(item_info)
                    elif status.safety_result and status.safety_result.has_concerns:
                        flagged_items.append(item_info)
                    else:
                        safe_items.append(item_info)

                return {
                    "success": True,
                    "total_items": len(cart_items),
                    "safe_count": len(safe_items),
                    "flagged_count": len(flagged_items),
                    "blocked_count": len(blocked_items),
                    "safe_items": safe_items,
                    "flagged_items": flagged_items,
                    "blocked_items": blocked_items,
                }

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
