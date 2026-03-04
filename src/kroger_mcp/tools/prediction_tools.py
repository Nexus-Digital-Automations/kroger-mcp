"""
Prediction and analytics tools for the Kroger MCP server.

Provides MCP tools for:
- Pantry inventory management
- Purchase predictions and recommendations
- Item categorization
- Statistics and analytics
- Shopping suggestions
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastmcp import Context
from pydantic import Field


def _get_session_id(ctx) -> str:
    """Extract session ID from MCP context."""
    if ctx and hasattr(ctx, 'session_id'):
        return str(ctx.session_id)
    return 'default'


def register_tools(mcp):
    """Register prediction and analytics tools with the FastMCP server."""

    @mcp.tool()
    async def pantry(
        action: Literal[
            "get",
            "add",
            "update_item",
            "restock",
            "get_low_inventory",
            "remove",
            "get_attention",
        ] = Field(
            description=(
                "Action: 'get' - view all pantry items with inventory levels, "
                "'add' - add item(s) to pantry tracking, "
                "'update_item' - manually set inventory level for item(s), "
                "'restock' - mark item(s) as restocked, "
                "'get_low_inventory' - get items running low, "
                "'remove' - remove item(s) from pantry tracking, "
                "'get_attention' - get items needing attention (expired, expiring, low, overdue)"
            )
        ),
        product_id: Optional[str] = Field(
            default=None,
            description="Product ID for single-item operations (add, update_item, restock, remove)",
        ),
        product_ids: Optional[List[str]] = Field(
            default=None,
            description="List of product IDs for batch operations (max 50) (add, update_item, restock, remove)",
        ),
        description: Optional[str] = Field(
            default=None,
            description="Product description (for add, applied to all items in batch if provided)",
        ),
        level: Optional[int] = Field(
            default=None,
            description="Inventory level 0-100% (for add default 100, update_item required, restock default 100)",
        ),
        low_threshold: Optional[int] = Field(
            default=None,
            description="Alert when level drops below this % (for add, default 20)",
        ),
        threshold: Optional[int] = Field(
            default=None,
            description="Threshold % to consider 'low' (for get_low_inventory, default 20)",
        ),
        days_ahead: Optional[int] = Field(
            default=None,
            description="Days ahead to check for expiring items (1-30, default 7, for get_attention)",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Pantry inventory management operations."""
        match action:
            case "get":
                try:
                    from ..analytics.pantry import get_pantry_status

                    items = get_pantry_status(apply_depletion=True)
                    return {
                        "success": True,
                        "items": items,
                        "count": len(items),
                        "low_count": sum(1 for i in items if i["status"] == "low"),
                        "out_count": sum(1 for i in items if i["status"] == "out"),
                        "timestamp": datetime.now().isoformat(),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get pantry: {str(e)}"}

            case "add":
                ids = product_ids if product_ids else ([product_id] if product_id else [])
                if not ids:
                    return {"success": False, "error": "product_id or product_ids is required"}
                is_batch = len(ids) > 1

                if len(ids) > 50:
                    return {"success": False, "error": "Maximum 50 products per batch request"}

                _level = level if level is not None else 100
                _low_threshold = low_threshold if low_threshold is not None else 20

                try:
                    from ..analytics.pantry import add_to_pantry as _add_to_pantry

                    results = {}
                    for pid in ids:
                        try:
                            result = _add_to_pantry(
                                product_id=pid,
                                description=description,
                                level=_level,
                                low_threshold=_low_threshold,
                                auto_deplete=True,
                            )
                            results[pid] = result
                        except Exception as e:
                            results[pid] = {"success": False, "error": f"Failed to add {pid}: {str(e)}"}

                    if is_batch:
                        success_count = sum(1 for r in results.values() if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(ids),
                                "successful": success_count,
                                "failed": len(ids) - success_count,
                                "level": _level,
                                "low_threshold": _low_threshold,
                            },
                        }
                    return results[ids[0]]
                except Exception as e:
                    return {"success": False, "error": f"Failed to add to pantry: {str(e)}"}

            case "update_item":
                ids = product_ids if product_ids else ([product_id] if product_id else [])
                if not ids:
                    return {"success": False, "error": "product_id or product_ids is required"}
                if level is None:
                    return {"success": False, "error": "level is required for update_item"}
                is_batch = len(ids) > 1

                if len(ids) > 50:
                    return {"success": False, "error": "Maximum 50 products per batch request"}

                try:
                    from ..analytics.pantry import update_pantry_level

                    results = {}
                    for pid in ids:
                        try:
                            results[pid] = update_pantry_level(pid, level)
                        except Exception as e:
                            results[pid] = {"success": False, "error": f"Failed to update {pid}: {str(e)}"}

                    if is_batch:
                        success_count = sum(1 for r in results.values() if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(ids),
                                "successful": success_count,
                                "failed": len(ids) - success_count,
                                "level_set": level,
                            },
                        }
                    return results[ids[0]]
                except Exception as e:
                    return {"success": False, "error": f"Failed to update pantry: {str(e)}"}

            case "restock":
                ids = product_ids if product_ids else ([product_id] if product_id else [])
                if not ids:
                    return {"success": False, "error": "product_id or product_ids is required"}
                is_batch = len(ids) > 1

                if len(ids) > 50:
                    return {"success": False, "error": "Maximum 50 products per batch request"}

                _level = level if level is not None else 100

                try:
                    from ..analytics.pantry import restock_item

                    results = {}
                    for pid in ids:
                        try:
                            results[pid] = restock_item(pid, _level)
                        except Exception as e:
                            results[pid] = {"success": False, "error": f"Failed to restock {pid}: {str(e)}"}

                    if is_batch:
                        success_count = sum(1 for r in results.values() if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(ids),
                                "successful": success_count,
                                "failed": len(ids) - success_count,
                                "level_set": _level,
                            },
                        }
                    return results[ids[0]]
                except Exception as e:
                    return {"success": False, "error": f"Failed to restock: {str(e)}"}

            case "get_low_inventory":
                _threshold = threshold if threshold is not None else 20
                try:
                    from ..analytics.pantry import get_low_inventory_items

                    items = get_low_inventory_items(_threshold)
                    return {
                        "success": True,
                        "threshold": _threshold,
                        "items": items,
                        "count": len(items),
                        "out_count": sum(1 for i in items if i["level_percent"] <= 0),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get low inventory: {str(e)}"}

            case "remove":
                ids = product_ids if product_ids else ([product_id] if product_id else [])
                if not ids:
                    return {"success": False, "error": "product_id or product_ids is required"}
                is_batch = len(ids) > 1

                if len(ids) > 50:
                    return {"success": False, "error": "Maximum 50 products per batch request"}

                try:
                    from ..analytics.pantry import remove_from_pantry as _remove_from_pantry

                    results = {}
                    for pid in ids:
                        try:
                            results[pid] = _remove_from_pantry(pid)
                        except Exception as e:
                            results[pid] = {"success": False, "error": f"Failed to remove {pid}: {str(e)}"}

                    if is_batch:
                        success_count = sum(1 for r in results.values() if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(ids),
                                "successful": success_count,
                                "failed": len(ids) - success_count,
                            },
                        }
                    return results[ids[0]]
                except Exception as e:
                    return {"success": False, "error": f"Failed to remove from pantry: {str(e)}"}

            case "get_attention":
                days_ahead_val = days_ahead if days_ahead is not None else 7
                try:
                    from ..analytics.pantry import get_pantry_status
                    from ..analytics.predictions import get_predictions_for_period
                    from ..config.session_state import get_session_manager

                    session_id = _get_session_id(ctx)
                    pantry_items = get_pantry_status(apply_depletion=True)
                    overdue_predictions = get_predictions_for_period(
                        days_ahead=0,
                        min_confidence=0.5,
                        include_overdue=True
                    )
                    overdue_ids = {p.product_id: p for p in overdue_predictions if p.days_until is not None and p.days_until < 0}
                    attention_items = []

                    for item in pantry_items:
                        pid = item['product_id']
                        reasons = []
                        urgency_level = None
                        action_msg = None
                        exp_status = item.get('expiration_status', 'none')
                        days_to_exp = item.get('days_to_expiration')
                        if exp_status == 'expired':
                            reasons.append('expired')
                            urgency_level = 'critical'
                            action_msg = f"Use immediately or discard - expired {abs(days_to_exp)} days ago"
                        elif exp_status == 'critical':
                            reasons.append('expiring_critical')
                            urgency_level = 'critical'
                            action_msg = f"Expires in {days_to_exp} days"
                        elif exp_status == 'warning' and days_to_exp is not None and days_to_exp <= days_ahead_val:
                            reasons.append('expiring_soon')
                            urgency_level = urgency_level or 'high'
                            action_msg = action_msg or f"Expiring soon ({days_to_exp} days)"
                        level = item.get('level_percent', 100)
                        status = item.get('status', 'ok')
                        if level <= 10:
                            reasons.append('critical_inventory')
                            urgency_level = 'critical'
                            action_msg = action_msg or f"Running critically low ({level}%) - reorder immediately"
                        elif level <= 25 and status == 'low':
                            reasons.append('low_inventory')
                            urgency_level = urgency_level or 'medium'
                            action_msg = action_msg or f"Running low ({level}%) - reorder soon"
                        if pid in overdue_ids:
                            pred = overdue_ids[pid]
                            days_overdue = abs(pred.days_until)
                            reasons.append('overdue')
                            urgency_level = urgency_level or 'medium'
                            action_msg = action_msg or f"Overdue by {days_overdue} days - time to repurchase"
                        if reasons:
                            attention_items.append({
                                "product_id": pid,
                                "description": item['description'],
                                "attention_reason": reasons[0],
                                "urgency_level": urgency_level,
                                "details": {
                                    "expiration_date": item.get('expiration_date'),
                                    "days_to_expiration": days_to_exp,
                                    "pantry_level": level,
                                    "days_overdue": abs(overdue_ids[pid].days_until) if pid in overdue_ids else 0,
                                    "days_until_empty": item.get('days_until_empty')
                                },
                                "action": action_msg
                            })

                    urgency_order = {'critical': 0, 'high': 1, 'medium': 2}
                    attention_items.sort(key=lambda x: urgency_order.get(x['urgency_level'], 3))
                    summary = {
                        "total_items": len(attention_items),
                        "expired": sum(1 for i in attention_items if i['attention_reason'] == 'expired'),
                        "expiring_critical": sum(1 for i in attention_items if i['attention_reason'] == 'expiring_critical'),
                        "expiring_soon": sum(1 for i in attention_items if i['attention_reason'] == 'expiring_soon'),
                        "critical_inventory": sum(1 for i in attention_items if i['attention_reason'] == 'critical_inventory'),
                        "low_inventory": sum(1 for i in attention_items if i['attention_reason'] == 'low_inventory'),
                        "overdue": sum(1 for i in attention_items if i['attention_reason'] == 'overdue')
                    }
                    session_manager = get_session_manager()
                    session_manager.mark_tool_called(session_id, "get_pantry_attention")
                    return {
                        "success": True,
                        "items": attention_items,
                        "summary": summary,
                        "timestamp": datetime.now().isoformat(),
                        "_session_requirement_fulfilled": True
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get pantry attention items: {str(e)}"}

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}

    @mcp.tool()
    async def predictions(
        action: Literal[
            "get_predictions",
            "get_item_stats",
            "categorize",
            "get_by_category",
            "get_history",
            "get_suggestions",
            "get_smart_recommendations",
            "get_seasonal",
            "migrate_data",
            "get_category_summary",
            "configure",
            "get_config",
            "reset_config",
        ] = Field(
            description=(
                "Action: 'get_predictions' - get items needing repurchase soon, "
                "'get_item_stats' - get purchase statistics for product(s), "
                "'categorize' - set category for product(s), "
                "'get_by_category' - get all items in a category, "
                "'get_history' - get purchase history for a product, "
                "'get_suggestions' - generate smart shopping list from patterns, "
                "'get_smart_recommendations' - comprehensive recommendations with scoring, "
                "'get_seasonal' - get upcoming seasonal/holiday items, "
                "'migrate_data' - migrate purchase data from JSON to database, "
                "'get_category_summary' - get counts per category, "
                "'configure' - update prediction parameters, "
                "'get_config' - view current prediction config, "
                "'reset_config' - reset prediction config to defaults"
            )
        ),
        days_ahead: Optional[int] = Field(
            default=None,
            description="Days to look ahead for predictions (for get_predictions default 14, get_suggestions default 7, get_smart_recommendations default 14, get_seasonal default 30)",
        ),
        category: Optional[str] = Field(
            default=None,
            description="Category filter: 'routine', 'regular', 'treat' (for get_predictions, get_by_category also accepts 'uncategorized', categorize single mode)",
        ),
        min_confidence: Optional[float] = Field(
            default=None,
            description="Minimum prediction confidence 0-1 (for get_predictions, default 0.5)",
        ),
        product_id: Optional[str] = Field(
            default=None,
            description="Product ID for single-item operations (get_item_stats, categorize, get_history)",
        ),
        product_ids: Optional[List[str]] = Field(
            default=None,
            description="List of product IDs for batch get_item_stats (max 20)",
        ),
        items: Optional[List[Dict[str, Any]]] = Field(
            default=None,
            description="Batch categorize list: [{product_id, category}, ...] max 50 (for categorize batch mode)",
        ),
        history_limit: Optional[int] = Field(
            default=None,
            description="Max history events to return 1-100 (for get_history, default 20)",
        ),
        include_routine: Optional[bool] = Field(
            default=None,
            description="Include routine items (for get_suggestions, default True)",
        ),
        include_predicted: Optional[bool] = Field(
            default=None,
            description="Include predicted items (for get_suggestions, default True)",
        ),
        include_seasonal: Optional[bool] = Field(
            default=None,
            description="Include seasonal items (for get_suggestions, default True)",
        ),
        include_low_pantry: Optional[bool] = Field(
            default=None,
            description="Include items with low inventory (for get_smart_recommendations, default True)",
        ),
        include_deals: Optional[bool] = Field(
            default=None,
            description="Prioritize items on sale (for get_smart_recommendations, default True)",
        ),
        include_predictions_flag: Optional[bool] = Field(
            default=None,
            description="Include consumption-based predictions (for get_smart_recommendations, default True)",
        ),
        include_favorites_only: Optional[bool] = Field(
            default=None,
            description="Only recommend items in favorites (for get_smart_recommendations, default False)",
        ),
        min_score: Optional[int] = Field(
            default=None,
            description="Filter items below this score 0-100 (for get_smart_recommendations, default 20)",
        ),
        max_results: Optional[int] = Field(
            default=None,
            description="Max recommendations to return 1-100 (for get_smart_recommendations, default 50)",
        ),
        holiday: Optional[str] = Field(
            default=None,
            description="Holiday filter: thanksgiving, christmas, halloween, easter, july_4th (for get_seasonal)",
        ),
        force: Optional[bool] = Field(
            default=None,
            description="Force re-migration even if already done (for migrate_data, default False)",
        ),
        ewma_alpha: Optional[float] = Field(
            default=None,
            description="EWMA decay factor 0.1-0.9 (for configure)",
        ),
        routine_buffer: Optional[float] = Field(
            default=None,
            description="Safety buffer for routine items std dev multiplier (for configure)",
        ),
        regular_buffer: Optional[float] = Field(
            default=None,
            description="Safety buffer for regular items std dev multiplier (for configure)",
        ),
        treat_buffer: Optional[float] = Field(
            default=None,
            description="Safety buffer for treat items std dev multiplier (for configure)",
        ),
        routine_max_days: Optional[int] = Field(
            default=None,
            description="Max days between purchases for 'routine' category (for configure)",
        ),
        regular_max_days: Optional[int] = Field(
            default=None,
            description="Max days between purchases for 'regular' category (for configure)",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Purchase predictions, analytics, and configuration operations."""
        match action:
            case "get_predictions":
                try:
                    from ..analytics.predictions import get_predictions_for_period

                    _days = days_ahead if days_ahead is not None else 14
                    _conf = min_confidence if min_confidence is not None else 0.5

                    preds = get_predictions_for_period(
                        days_ahead=_days,
                        category_filter=category,
                        min_confidence=_conf,
                        include_overdue=True,
                    )
                    return {
                        "success": True,
                        "predictions": [
                            {
                                "product_id": p.product_id,
                                "description": p.description,
                                "category": p.category,
                                "predicted_date": (
                                    p.predicted_date.isoformat() if p.predicted_date else None
                                ),
                                "days_until": p.days_until,
                                "urgency": p.urgency,
                                "urgency_label": p.urgency_label,
                                "confidence": p.confidence,
                                "last_purchased": p.last_purchase_date,
                                "avg_days_between": p.avg_days_between,
                            }
                            for p in preds
                        ],
                        "count": len(preds),
                        "urgent_count": sum(1 for p in preds if p.urgency >= 0.7),
                        "overdue_count": sum(
                            1 for p in preds if p.days_until is not None and p.days_until < 0
                        ),
                        "timestamp": datetime.now().isoformat(),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get predictions: {str(e)}"}

            case "get_item_stats":
                ids = product_ids if product_ids else ([product_id] if product_id else [])
                if not ids:
                    return {"success": False, "error": "product_id or product_ids is required"}
                is_batch = len(ids) > 1

                if len(ids) > 20:
                    return {"success": False, "error": "Maximum 20 products per batch request"}

                try:
                    from ..analytics.statistics import get_product_statistics
                    from ..analytics.predictions import predict_repurchase_date
                    from ..analytics.purchase_tracker import get_purchase_events

                    def get_stats_for(pid: str) -> Dict[str, Any]:
                        stats = get_product_statistics(pid)
                        if not stats:
                            return {"success": False, "error": f"No statistics found for product {pid}"}
                        prediction = predict_repurchase_date(pid, stats)
                        events = get_purchase_events(pid, "order_placed", limit=10)
                        return {
                            "success": True,
                            "product_id": pid,
                            "description": stats.get("description"),
                            "brand": stats.get("brand"),
                            "category": stats.get("category_type"),
                            "is_manual_category": bool(stats.get("category_override")),
                            "statistics": {
                                "total_purchases": stats.get("total_purchases"),
                                "total_quantity": stats.get("total_quantity"),
                                "avg_quantity_per_purchase": round(
                                    stats.get("avg_quantity_per_purchase") or 0, 2
                                ),
                                "avg_days_between_purchases": round(
                                    stats.get("avg_days_between_purchases") or 0, 1
                                ),
                                "std_dev_days": round(stats.get("std_dev_days") or 0, 1),
                                "first_purchase": stats.get("first_purchase_date"),
                                "last_purchase": stats.get("last_purchase_date"),
                                "purchase_frequency_score": round(
                                    stats.get("purchase_frequency_score") or 0, 3
                                ),
                                "seasonality_score": round(
                                    stats.get("seasonality_score") or 0, 2
                                ),
                            },
                            "prediction": {
                                "next_purchase_date": (
                                    prediction.predicted_date.isoformat()
                                    if prediction.predicted_date
                                    else None
                                ),
                                "days_until": prediction.days_until,
                                "urgency": prediction.urgency,
                                "urgency_label": prediction.urgency_label,
                                "confidence": prediction.confidence,
                            },
                            "recent_purchases": [
                                {
                                    "date": e.get("event_date"),
                                    "quantity": e.get("quantity"),
                                    "modality": e.get("modality"),
                                }
                                for e in events
                            ],
                        }

                    results = {pid: get_stats_for(pid) for pid in ids}

                    if is_batch:
                        success_count = sum(1 for r in results.values() if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(ids),
                                "successful": success_count,
                                "failed": len(ids) - success_count,
                            },
                        }
                    return results[ids[0]]
                except Exception as e:
                    return {"success": False, "error": f"Failed to get statistics: {str(e)}"}

            case "categorize":
                valid_categories = ["routine", "regular", "treat"]

                if items is not None:
                    if len(items) > 50:
                        return {"success": False, "error": "Maximum 50 products per batch request"}
                    for item in items:
                        if "product_id" not in item or "category" not in item:
                            return {
                                "success": False,
                                "error": "Each item must have 'product_id' and 'category' fields",
                            }
                        if item["category"] not in valid_categories:
                            return {
                                "success": False,
                                "error": f"Invalid category '{item['category']}'. Must be one of: {valid_categories}",
                            }
                    is_batch = True
                    items_to_process = items
                else:
                    if not product_id or not category:
                        return {
                            "success": False,
                            "error": "Single mode requires both product_id and category parameters",
                        }
                    if category not in valid_categories:
                        return {
                            "success": False,
                            "error": f"Invalid category. Must be one of: {valid_categories}",
                        }
                    is_batch = False
                    items_to_process = [{"product_id": product_id, "category": category}]

                try:
                    from ..analytics.categories import set_product_category

                    results = {}
                    for item in items_to_process:
                        pid = item["product_id"]
                        cat = item["category"]
                        try:
                            result = set_product_category(pid, cat, is_override=True)
                            results[pid] = {
                                "success": True,
                                "product_id": pid,
                                "category": cat,
                                "previous_category": result.previous_category,
                                "was_auto_detected": not result.was_override,
                                "message": f"Category set to '{cat}' for product {pid}",
                            }
                        except Exception as e:
                            results[pid] = {
                                "success": False,
                                "error": f"Failed to categorize {pid}: {str(e)}",
                            }

                    if is_batch:
                        success_count = sum(1 for r in results.values() if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(items_to_process),
                                "successful": success_count,
                                "failed": len(items_to_process) - success_count,
                            },
                        }
                    return results[items_to_process[0]["product_id"]]
                except Exception as e:
                    return {"success": False, "error": f"Failed to set category: {str(e)}"}

            case "get_by_category":
                if not category:
                    return {"success": False, "error": "category is required"}
                valid_categories = ["routine", "regular", "treat", "uncategorized"]
                if category not in valid_categories:
                    return {
                        "success": False,
                        "error": f"Invalid category. Must be one of: {valid_categories}",
                    }
                try:
                    from ..analytics.categories import get_items_by_category

                    cat_items = get_items_by_category(category, include_stats=True)
                    return {
                        "success": True,
                        "category": category,
                        "items": [
                            {
                                "product_id": item.get("product_id"),
                                "description": item.get("description"),
                                "brand": item.get("brand"),
                                "total_purchases": item.get("total_purchases"),
                                "avg_days_between": round(
                                    item.get("avg_days_between_purchases") or 0, 1
                                ),
                                "last_purchase": item.get("last_purchase_date"),
                                "seasonality_score": round(
                                    item.get("seasonality_score") or 0, 2
                                ),
                            }
                            for item in cat_items
                        ],
                        "count": len(cat_items),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get items: {str(e)}"}

            case "get_history":
                if not product_id:
                    return {"success": False, "error": "product_id is required"}
                _limit = history_limit if history_limit is not None else 20
                try:
                    from ..analytics.purchase_tracker import get_purchase_events

                    events = get_purchase_events(product_id, event_type="order_placed", limit=_limit)
                    return {
                        "success": True,
                        "product_id": product_id,
                        "events": [
                            {
                                "date": e.get("event_date"),
                                "timestamp": e.get("event_timestamp"),
                                "quantity": e.get("quantity"),
                                "modality": e.get("modality"),
                                "order_id": e.get("order_id"),
                            }
                            for e in events
                        ],
                        "count": len(events),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get history: {str(e)}"}

            case "get_suggestions":
                try:
                    from ..analytics.predictions import get_shopping_suggestions

                    _days = days_ahead if days_ahead is not None else 7
                    _routine = include_routine if include_routine is not None else True
                    _predicted = include_predicted if include_predicted is not None else True
                    _seasonal = include_seasonal if include_seasonal is not None else True

                    suggestions = get_shopping_suggestions(
                        include_routine=_routine,
                        include_predicted=_predicted,
                        include_seasonal=_seasonal,
                        days_ahead=_days,
                        min_confidence=0.5,
                    )
                    return {"success": True, **suggestions, "timestamp": datetime.now().isoformat()}
                except Exception as e:
                    return {"success": False, "error": f"Failed to get suggestions: {str(e)}"}

            case "get_smart_recommendations":
                try:
                    from ..analytics.recommendations import get_comprehensive_recommendations

                    _days = days_ahead if days_ahead is not None else 14
                    _low_pantry = include_low_pantry if include_low_pantry is not None else True
                    _deals = include_deals if include_deals is not None else True
                    _preds = include_predictions_flag if include_predictions_flag is not None else True
                    _fav_only = include_favorites_only if include_favorites_only is not None else False
                    _min_score = min_score if min_score is not None else 20
                    _max_res = max_results if max_results is not None else 50

                    location_id = None
                    try:
                        from ..storage import get_preferred_location
                        location = get_preferred_location()
                        if location:
                            location_id = location.get("location_id")
                    except Exception:
                        pass

                    return get_comprehensive_recommendations(
                        days_ahead=_days,
                        include_low_pantry=_low_pantry,
                        include_deals=_deals,
                        include_predictions=_preds,
                        include_favorites_only=_fav_only,
                        min_score=_min_score,
                        max_results=_max_res,
                        location_id=location_id,
                    )
                except Exception as e:
                    return {"success": False, "error": f"Failed to get smart recommendations: {str(e)}"}

            case "get_seasonal":
                try:
                    _days = days_ahead if days_ahead is not None else 30
                    if holiday:
                        from ..analytics.seasonal import get_holiday_items
                        sea_items = get_holiday_items(holiday)
                        return {"success": True, "holiday": holiday, "items": sea_items, "count": len(sea_items)}
                    else:
                        from ..analytics.seasonal import get_upcoming_seasonal_items
                        sea_items = get_upcoming_seasonal_items(_days)
                        return {"success": True, "days_ahead": _days, "items": sea_items, "count": len(sea_items)}
                except Exception as e:
                    return {"success": False, "error": f"Failed to get seasonal items: {str(e)}"}

            case "migrate_data":
                try:
                    from ..analytics.migration import (
                        force_remigration,
                        get_migration_status,
                        migrate_json_to_sqlite,
                    )

                    _force = force if force is not None else False
                    result = force_remigration() if _force else migrate_json_to_sqlite()
                    status = get_migration_status()
                    return {
                        "success": result.get("success", False),
                        "migration_result": result,
                        "current_status": status,
                    }
                except Exception as e:
                    return {"success": False, "error": f"Migration failed: {str(e)}"}

            case "get_category_summary":
                try:
                    from ..analytics.categories import get_category_summary

                    summary = get_category_summary()
                    total = sum(summary.values())
                    return {
                        "success": True,
                        "categories": summary,
                        "total_products": total,
                        "timestamp": datetime.now().isoformat(),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to get summary: {str(e)}"}

            case "configure":
                try:
                    from ..analytics.config import get_config_summary, update_config

                    kwargs = {}
                    if ewma_alpha is not None:
                        kwargs["ewma_alpha"] = ewma_alpha
                    if routine_buffer is not None:
                        kwargs["buffer_routine"] = routine_buffer
                    if regular_buffer is not None:
                        kwargs["buffer_regular"] = regular_buffer
                    if treat_buffer is not None:
                        kwargs["buffer_treat"] = treat_buffer
                    if routine_max_days is not None:
                        kwargs["routine_max_days"] = routine_max_days
                    if regular_max_days is not None:
                        kwargs["regular_max_days"] = regular_max_days

                    result = update_config(**kwargs) if kwargs else {"success": True, "message": "No changes specified"}
                    result["current_config"] = get_config_summary()
                    return result
                except Exception as e:
                    return {"success": False, "error": f"Failed to configure predictions: {str(e)}"}

            case "get_config":
                try:
                    from ..analytics.config import get_config_summary

                    return {"success": True, "config": get_config_summary()}
                except Exception as e:
                    return {"success": False, "error": f"Failed to get config: {str(e)}"}

            case "reset_config":
                try:
                    from ..analytics.config import get_config_summary, reset_config

                    reset_config()
                    return {
                        "success": True,
                        "message": "Configuration reset to defaults",
                        "config": get_config_summary(),
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to reset config: {str(e)}"}

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
