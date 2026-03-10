"""API routes for analytics and reports."""
import json
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter()


def _serializable(obj):
    """Recursively convert sets to lists so the dict is JSON-serializable."""
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, dict):
        return {k: _serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serializable(v) for v in obj]
    return obj


@router.get("/api/analytics/spending")
async def get_spending_report(days: int = Query(default=30, ge=1, le=365)):
    """Generate a spending / purchase analytics report."""
    try:
        from kroger_mcp.analytics.reporting import generate_spending_report
        report = generate_spending_report(days_back=days)
        # Normalise key so the frontend always sees 'category_breakdown'
        if "by_category" in report and "category_breakdown" not in report:
            report["category_breakdown"] = report.pop("by_category")
        return _serializable(report)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )


@router.get("/api/analytics/patterns")
async def get_patterns_report(days: int = Query(default=30, ge=1, le=365)):
    """Generate a shopping-behaviour patterns report."""
    try:
        from kroger_mcp.analytics.reporting import generate_patterns_report
        report = generate_patterns_report(days_back=days)
        return _serializable(report)
    except ImportError:
        pass
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )

    # Fallback: return basic purchase-event data
    try:
        from kroger_mcp.analytics.database import get_db_connection, ensure_initialized
        from datetime import datetime, timedelta
        ensure_initialized()
        start_date = (
            datetime.now() - timedelta(days=days)
        ).strftime("%Y-%m-%d")
        conn = get_db_connection()
        try:
            cursor = conn.execute(
                """
                SELECT product_id, COUNT(*) as count
                FROM purchase_events
                WHERE event_type = 'order_placed' AND event_date >= ?
                GROUP BY product_id
                ORDER BY count DESC
                LIMIT 20
                """,
                (start_date,),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()
        return {"period": f"Last {days} days", "top_products": rows}
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )


@router.get("/api/analytics/pantry-report")
async def get_pantry_report():
    """Return a pantry-level report."""
    try:
        from kroger_mcp.analytics.reporting import generate_pantry_report
        report = generate_pantry_report()
        return _serializable(report)
    except (ImportError, AttributeError):
        pass
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )

    # Fallback: call pantry status directly
    try:
        from kroger_mcp.analytics.pantry import get_pantry_status
        report = get_pantry_status()
        return _serializable(report)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )


@router.get("/api/analytics/cookable-recipes")
async def get_cookable_recipes():
    """
    Return recipes that can be (fully or mostly) cooked from current pantry stock.
    """
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes
        from kroger_mcp.analytics.pantry import get_pantry_status

        data = _load_recipes()
        recipes = data.get("recipes", [])
        pantry_info = get_pantry_status()

        # Build a set of product_ids that are sufficiently stocked (> 10%)
        stocked_ids: set = set()
        for item in pantry_info:
            level = item.get("level_percent")
            if level is None or level > 10:
                stocked_ids.add(item.get("product_id"))

        results = []
        for recipe in recipes:
            ingredients = recipe.get("ingredients", [])
            if not ingredients:
                continue
            total = len(ingredients)
            # Only count ingredients that are linked to a Kroger product
            linked = [
                ing for ing in ingredients
                if ing.get("product_id") and not ing.get("override")
            ]
            if not linked:
                continue
            available = sum(
                1 for ing in linked
                if ing.get("product_id") in stocked_ids
            )
            pct = round(available / len(linked) * 100) if linked else 0
            if pct >= 50:
                results.append({
                    "id": recipe.get("id"),
                    "name": recipe.get("name"),
                    "cookable_percent": pct,
                    "available_ingredients": available,
                    "total_linked_ingredients": len(linked),
                    "total_ingredients": total,
                })

        results.sort(key=lambda r: r["cookable_percent"], reverse=True)
        return {"recipes": results, "count": len(results)}
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )


@router.get("/api/analytics/export")
async def export_all_data():
    """Export all app data as a downloadable JSON file."""
    try:
        payload: dict = {}

        # Recipes
        try:
            from kroger_mcp.tools.recipe_tools import _load_recipes
            payload["recipes"] = _load_recipes()
        except Exception:
            payload["recipes"] = {}

        # Pantry
        try:
            from kroger_mcp.analytics.pantry import get_pantry_status
            payload["pantry"] = get_pantry_status()
        except Exception:
            payload["pantry"] = {}

        # Cart
        try:
            from kroger_mcp.tools.cart_tools import _load_cart_data
            payload["cart"] = _load_cart_data()
        except Exception:
            payload["cart"] = {}

        # Shopping list
        try:
            import json as _json
            import os
            from pathlib import Path
            sl_file = str(
                Path(__file__).parent.parent.parent.parent.parent.parent
                / "kroger_shopping_list.json"
            )
            if os.path.exists(sl_file):
                with open(sl_file) as f:
                    payload["shopping_list"] = _json.load(f)
        except Exception:
            payload["shopping_list"] = {}

        serialized = _serializable(payload)
        content = json.dumps(serialized, indent=2)
        return JSONResponse(
            content=serialized,
            headers={
                "Content-Disposition": "attachment; filename=kroger_export.json",
                "Content-Type": "application/json",
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )
