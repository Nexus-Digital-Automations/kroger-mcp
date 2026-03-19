"""Pantry API endpoints — write operations for the pantry dashboard."""
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from kroger_mcp.analytics.pantry import (
    add_to_pantry,
    remove_from_pantry,
    restock_item,
    update_pantry_level,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class UpdateLevelRequest(BaseModel):
    product_id: str
    level_percent: int = Field(..., ge=0, le=100)


class AddItemRequest(BaseModel):
    product_id: str
    description: Optional[str] = None
    level_percent: int = Field(100, ge=0, le=100)


class RestockRequest(BaseModel):
    product_id: str
    level: int = Field(100, ge=0, le=100)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post('/api/pantry/update')
async def update_pantry_item_level(body: UpdateLevelRequest):
    """Update an existing pantry item's level percentage."""
    try:
        result = update_pantry_level(body.product_id, body.level_percent)
        if not result.get('success'):
            return JSONResponse(
                status_code=404,
                content={'error': result.get('error', 'Item not found')},
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to update pantry level: {str(e)}'},
        )


@router.post('/api/pantry/add')
async def add_pantry_item(body: AddItemRequest):
    """Add a new item to pantry tracking (or update existing)."""
    try:
        result = add_to_pantry(
            product_id=body.product_id,
            description=body.description,
            level=body.level_percent,
        )
        if not result.get('success'):
            return JSONResponse(
                status_code=400,
                content={'error': result.get('error', 'Failed to add item')},
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to add pantry item: {str(e)}'},
        )


@router.delete('/api/pantry')
async def clear_all_pantry_items():
    """Remove all items from pantry tracking."""
    try:
        from kroger_mcp.analytics.database import get_db_cursor, ensure_initialized
        ensure_initialized()
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM pantry_items")
            deleted = cursor.rowcount
        return JSONResponse(content={"success": True, "deleted": deleted})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to clear pantry: {str(e)}'},
        )


@router.delete('/api/pantry/{product_id}')
async def delete_pantry_item(product_id: str):
    """Remove an item from pantry tracking."""
    try:
        result = remove_from_pantry(product_id)
        if not result.get('success'):
            return JSONResponse(
                status_code=404,
                content={'error': result.get('error', 'Item not found')},
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to remove pantry item: {str(e)}'},
        )


@router.post('/api/pantry/restock')
async def restock_pantry_item(body: RestockRequest):
    """Restock a pantry item to the specified level (default 100%)."""
    try:
        result = restock_item(product_id=body.product_id, level=body.level)
        if not result.get('success'):
            return JSONResponse(
                status_code=400,
                content={'error': result.get('error', 'Failed to restock item')},
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to restock pantry item: {str(e)}'},
        )
