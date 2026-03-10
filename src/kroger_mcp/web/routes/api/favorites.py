"""API routes for favorites list write operations."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

router = APIRouter()


class CreateListBody(BaseModel):
    name: str
    description: Optional[str] = None
    list_type: str = "custom"
    reorder_weeks: Optional[int] = None


class RenameListBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class AddItemBody(BaseModel):
    product_id: str
    description: str
    brand: Optional[str] = None
    quantity: int = 1
    notes: Optional[str] = None


@router.post("/api/favorites/lists")
async def create_list(body: CreateListBody):
    """Create a new favorite list."""
    try:
        from kroger_mcp.analytics.favorites import create_list as _create_list
        result = _create_list(
            name=body.name,
            description=body.description,
            list_type=body.list_type,
            reorder_weeks=body.reorder_weeks,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=400,
                content=result,
            )
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/favorites/lists/{list_id}")
async def delete_list(list_id: str):
    """Delete a favorite list and all its items."""
    try:
        from kroger_mcp.analytics.favorites import delete_list as _delete_list
        result = _delete_list(list_id=list_id)
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.put("/api/favorites/lists/{list_id}")
async def rename_list(list_id: str, body: RenameListBody):
    """Rename a list or update its description."""
    try:
        from kroger_mcp.analytics.favorites import rename_list as _rename_list
        result = _rename_list(
            list_id=list_id,
            new_name=body.name,
            new_description=body.description,
        )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/favorites/lists/{list_id}/items")
async def get_list_items(list_id: str):
    """Get items in a favorite list."""
    try:
        from kroger_mcp.analytics.favorites import get_list_items as _get_list_items
        result = _get_list_items(list_id=list_id)
        if not result.get("success"):
            return JSONResponse(status_code=404, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/favorites/lists/{list_id}/items")
async def add_item(list_id: str, body: AddItemBody):
    """Add a product to a favorite list."""
    try:
        from kroger_mcp.analytics.favorites import add_to_list
        result = add_to_list(
            list_id=list_id,
            product_id=body.product_id,
            description=body.description,
            brand=body.brand,
            default_quantity=body.quantity,
            notes=body.notes,
        )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/favorites/lists/{list_id}/items/{product_id}")
async def remove_item(list_id: str, product_id: str):
    """Remove a product from a favorite list."""
    try:
        from kroger_mcp.analytics.favorites import remove_from_list
        result = remove_from_list(list_id=list_id, product_id=product_id)
        if not result.get("success"):
            return JSONResponse(status_code=404, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
