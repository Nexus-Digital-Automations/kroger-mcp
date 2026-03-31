"""Meal tracker API endpoints — log meals and track consumption."""
from typing import List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from kroger_mcp.analytics.meal_tracker import (
    delete_meal_log,
    get_meal_log,
    get_today_meals,
    log_meal,
)
from kroger_mcp.analytics.pantry import get_pantry_status

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class LogMealItem(BaseModel):
    product_id: str
    description: Optional[str] = None
    quantity_percent: float = Field(10.0, ge=0.0, le=100.0)


class LogMealRequest(BaseModel):
    meal_type: str
    items: List[LogMealItem]
    description: Optional[str] = None
    recipe_id: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post('/api/meal-tracker/log')
async def api_log_meal(body: LogMealRequest):
    """Log a meal or snack with pantry deduction."""
    try:
        result = log_meal(
            meal_type=body.meal_type,
            items=[i.model_dump() for i in body.items],
            description=body.description,
            recipe_id=body.recipe_id,
            notes=body.notes,
        )
        if not result.get('success'):
            return JSONResponse(status_code=400, content=result)
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to log meal: {str(e)}'},
        )


@router.get('/api/meal-tracker/log')
async def api_get_meal_log(request: Request):
    """Get meal log with optional date filters."""
    try:
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        result = get_meal_log(date_from=date_from, date_to=date_to)
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to get meal log: {str(e)}'},
        )


@router.delete('/api/meal-tracker/log/{log_id}')
async def api_delete_meal_log(log_id: int):
    """Delete a meal log entry and restore pantry levels."""
    try:
        result = delete_meal_log(log_id)
        if not result.get('success'):
            return JSONResponse(status_code=404, content=result)
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to delete meal log: {str(e)}'},
        )


@router.get('/api/meal-tracker/today')
async def api_get_today_meals():
    """Get today's meals summary with stats."""
    try:
        result = get_today_meals()
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to get today meals: {str(e)}'},
        )


@router.get('/api/meal-tracker/pantry')
async def api_get_pantry_for_tracker():
    """Get current pantry items for the meal tracker picker."""
    try:
        items = get_pantry_status(apply_depletion=True)
        return JSONResponse(content={'items': items})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to get pantry: {str(e)}'},
        )
