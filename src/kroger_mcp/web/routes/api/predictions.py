"""Predictions API endpoints."""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter()


def _prediction_to_dict(pred) -> dict:
    """Convert a RepurchasePrediction dataclass to a JSON-serializable dict."""
    if hasattr(pred, "__dataclass_fields__"):
        return {
            "product_id": pred.product_id,
            "description": pred.description,
            "category_type": pred.category,
            "predicted_date": pred.predicted_date.isoformat() if pred.predicted_date else None,
            "days_until": pred.days_until,
            "urgency": pred.urgency,
            "urgency_label": pred.urgency_label,
            "confidence": pred.confidence,
            "last_purchase_date": pred.last_purchase_date,
            "avg_days_between": pred.avg_days_between,
        }
    if isinstance(pred, dict):
        return pred
    return {}


@router.get("/api/predictions")
async def get_predictions(days: int = Query(default=14, ge=1, le=365)):
    """Get repurchase predictions for the next N days."""
    try:
        from kroger_mcp.analytics.predictions import get_predictions_for_period

        raw = get_predictions_for_period(days_ahead=days)
        predictions = [_prediction_to_dict(p) for p in raw]
        return JSONResponse(content={"predictions": predictions, "count": len(predictions)})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get predictions: {str(e)}", "predictions": []},
        )


@router.get("/api/predictions/smart")
async def get_smart_recommendations():
    """Get smart shopping recommendations (if available)."""
    try:
        from kroger_mcp.analytics.recommendations import get_comprehensive_recommendations

        result = get_comprehensive_recommendations()
        if isinstance(result, list):
            return JSONResponse(content={"recommendations": result})
        if isinstance(result, dict):
            return JSONResponse(content=result)
        return JSONResponse(content={"recommendations": []})
    except ImportError:
        return JSONResponse(content={"recommendations": []})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get recommendations: {str(e)}", "recommendations": []},
        )
