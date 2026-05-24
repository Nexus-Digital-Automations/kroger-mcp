"""Pantry API endpoints — write operations for the pantry dashboard."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from kroger_mcp.analytics.pantry import (
    add_to_pantry,
    get_usage_history,
    list_pending_gaps,
    remove_from_pantry,
    resolve_gap,
    restock_item,
    update_pantry_level,
)
from kroger_mcp.auth.dependencies import current_user_id

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class UpdateLevelRequest(BaseModel):
    product_id: str
    level_percent: int = Field(..., ge=0, le=100)


class AddItemRequest(BaseModel):
    product_id: str
    description: str | None = None
    level_percent: int = Field(100, ge=0, le=100)
    quantity: float | None = None
    unit: str | None = None


class RestockRequest(BaseModel):
    product_id: str
    level: int = Field(100, ge=0, le=100)
    quantity: float | None = None
    unit: str | None = None


class ResolveGapRequest(BaseModel):
    resolution: str  # validated against pantry.resolve_gap's whitelist


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/pantry/update")
async def update_pantry_item_level(body: UpdateLevelRequest, request: Request):
    """Update an existing pantry item's level percentage."""
    user_id = current_user_id(request)
    try:
        result = update_pantry_level(body.product_id, body.level_percent, user_id=user_id)
        if not result.get("success"):
            return JSONResponse(
                status_code=404,
                content={"error": result.get("error", "Item not found")},
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to update pantry level: {str(e)}"},
        )


@router.post("/api/pantry/add")
async def add_pantry_item(body: AddItemRequest, request: Request):
    """Add a new item to pantry tracking (or update existing)."""
    user_id = current_user_id(request)
    try:
        result = add_to_pantry(
            product_id=body.product_id,
            description=body.description,
            level=body.level_percent,
            user_id=user_id,
            quantity=body.quantity,
            unit=body.unit,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=400,
                content={"error": result.get("error", "Failed to add item")},
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to add pantry item: {str(e)}"},
        )


@router.delete("/api/pantry")
async def clear_all_pantry_items(request: Request):
    """Remove all items from the current user's pantry. Requires ?confirmed=true."""
    if request.query_params.get("confirmed", "").lower() != "true":
        return JSONResponse(
            status_code=400,
            content={
                "error": "This will permanently delete all pantry items. Pass ?confirmed=true to proceed."
            },
        )
    user_id = current_user_id(request)
    try:
        from kroger_mcp.analytics.database import ensure_initialized, get_db_cursor

        ensure_initialized()
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM pantry_items WHERE user_id = ?", (user_id,))
            deleted = cursor.rowcount
        return JSONResponse(content={"success": True, "deleted": deleted})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to clear pantry: {str(e)}"},
        )


@router.delete("/api/pantry/{product_id}")
async def delete_pantry_item(product_id: str, request: Request):
    """Remove an item from pantry tracking."""
    user_id = current_user_id(request)
    try:
        result = remove_from_pantry(product_id, user_id=user_id)
        if not result.get("success"):
            return JSONResponse(
                status_code=404,
                content={"error": result.get("error", "Item not found")},
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to remove pantry item: {str(e)}"},
        )


@router.post("/api/pantry/restock")
async def restock_pantry_item(body: RestockRequest, request: Request):
    """Restock a pantry item to the specified level (default 100%)."""
    user_id = current_user_id(request)
    try:
        result = restock_item(
            product_id=body.product_id,
            level=body.level,
            user_id=user_id,
            quantity=body.quantity,
            unit=body.unit,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=400,
                content={"error": result.get("error", "Failed to restock item")},
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to restock pantry item: {str(e)}"},
        )


@router.get("/api/pantry/{product_id}/history")
async def get_pantry_item_history(product_id: str, request: Request, days: int = 30):
    """Recent purchase_events for one pantry item — powers sparkline + drawer."""
    user_id = current_user_id(request)
    try:
        events = get_usage_history(product_id=product_id, days=days, user_id=user_id)
        return JSONResponse(content={"product_id": product_id, "days": days, "events": events})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to fetch history: {str(e)}"},
        )


@router.get("/api/pantry/gaps")
async def list_gaps(request: Request):
    """Open gap-reconciliation rows for the pantry inbox."""
    user_id = current_user_id(request)
    try:
        gaps = list_pending_gaps(user_id=user_id)
        return JSONResponse(content={"gaps": gaps, "count": len(gaps)})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to list gaps: {str(e)}"},
        )


@router.post("/api/pantry/gaps/{gap_id}/resolve")
async def resolve_pantry_gap(gap_id: int, body: ResolveGapRequest, request: Request):
    """Close one gap; pantry_covered also writes a consumption event."""
    user_id = current_user_id(request)
    try:
        result = resolve_gap(gap_id=gap_id, resolution=body.resolution, user_id=user_id)
        if not result.get("success"):
            return JSONResponse(
                status_code=400,
                content={"error": result.get("error", "Failed to resolve gap")},
            )
        return JSONResponse(content=result)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to resolve gap: {str(e)}"},
        )
