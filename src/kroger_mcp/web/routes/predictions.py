"""Predictions page route."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/predictions", response_class=HTMLResponse)
async def predictions_page(request: Request):
    predictions = []
    try:
        from kroger_mcp.analytics.predictions import get_predictions_for_period

        # Parameter is days_ahead (not days)
        raw = get_predictions_for_period(days_ahead=14)
        if isinstance(raw, list):
            for pred in raw:
                # RepurchasePrediction is a dataclass — convert to dict
                if hasattr(pred, "__dataclass_fields__"):
                    d = {
                        "product_id": pred.product_id,
                        "description": pred.description,
                        "category_type": pred.category,
                        "predicted_date": (
                            pred.predicted_date.isoformat() if pred.predicted_date else None
                        ),
                        "days_until": pred.days_until,
                        "urgency": pred.urgency,
                        "urgency_label": pred.urgency_label,
                        "confidence": pred.confidence,
                        "last_purchase_date": pred.last_purchase_date,
                        "avg_days_between": pred.avg_days_between,
                    }
                    predictions.append(d)
                elif isinstance(pred, dict):
                    predictions.append(pred)
    except Exception:
        pass

    return templates.TemplateResponse(request, "predictions.html",
        {
            "active_page": "predictions",
            "predictions": predictions,
            "prediction_count": len(predictions),
        },
    )
