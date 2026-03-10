"""Safety API endpoints — settings, approved products, and blocked products."""
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics.database import ensure_initialized
from kroger_mcp.analytics.safety import (
    get_safety_settings,
    update_safety_settings,
    get_safe_products,
    add_to_safe_list,
    remove_from_safe_list,
    get_blocked_products,
    add_to_blocked_list,
    remove_from_blocked_list,
)
from kroger_mcp.analytics.ingredients import get_all_ingredients, get_active_ingredients

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SettingsRequest(BaseModel):
    filtering_enabled: Optional[bool] = None
    block_mode: Optional[str] = None


class ApproveProductRequest(BaseModel):
    product_id: str
    description: Optional[str] = None
    brand: Optional[str] = None
    reason: Optional[str] = None


class BlockProductRequest(BaseModel):
    product_id: str
    description: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

@router.get("/api/safety/settings")
async def get_settings():
    """Get current safety filter settings."""
    try:
        ensure_initialized()
        settings = get_safety_settings()
        return JSONResponse(content=settings)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get settings: {str(e)}"},
        )


@router.post("/api/safety/settings")
async def update_settings(body: SettingsRequest):
    """Update safety filter settings."""
    try:
        ensure_initialized()
        result = update_safety_settings(
            filtering_enabled=body.filtering_enabled,
            block_mode=body.block_mode,
        )
        return JSONResponse(content=result)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to update settings: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# Ingredients endpoint
# ---------------------------------------------------------------------------

@router.get("/api/safety/ingredients")
async def list_ingredients():
    """Get all flagged ingredients (system + custom)."""
    try:
        ensure_initialized()
        # get_active_ingredients returns system + custom merged
        ingredients = get_active_ingredients(include_custom=True)
        # Normalize to consistent shape for the UI
        result = [
            {
                "name": ing.get("name", ""),
                "severity": ing.get("severity", "watch"),
                "category": ing.get("category", ""),
                "reason": ing.get("reason", ""),
                "is_custom": ing.get("source") == "custom",
            }
            for ing in ingredients
        ]
        return JSONResponse(content=result)
    except Exception as e:
        # Fallback: return hardcoded ingredients only
        try:
            result = [
                {
                    "name": ing["name"],
                    "severity": ing["severity"],
                    "category": ing["category"],
                    "reason": ing["reason"],
                    "is_custom": False,
                }
                for ing in get_all_ingredients()
            ]
            return JSONResponse(content=result)
        except Exception:
            return JSONResponse(
                status_code=500,
                content={"error": f"Failed to get ingredients: {str(e)}"},
            )


# ---------------------------------------------------------------------------
# Safe products endpoints
# ---------------------------------------------------------------------------

@router.get("/api/safety/approved")
async def list_approved():
    """Get all safe-listed products."""
    try:
        ensure_initialized()
        products = get_safe_products()
        return JSONResponse(content=products)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get safe products: {str(e)}"},
        )


@router.post("/api/safety/approved")
async def approve_product(body: ApproveProductRequest):
    """Add a product to the safe list."""
    try:
        ensure_initialized()
        result = add_to_safe_list(
            product_id=body.product_id,
            description=body.description,
            brand=body.brand,
            reason=body.reason,
        )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to approve product: {str(e)}"},
        )


@router.delete("/api/safety/approved/{product_id}")
async def unapprove_product(product_id: str):
    """Remove a product from the safe list."""
    try:
        ensure_initialized()
        result = remove_from_safe_list(product_id)
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to remove product: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# Blocked products endpoints
# ---------------------------------------------------------------------------

@router.get("/api/safety/blocked")
async def list_blocked():
    """Get all blocked products."""
    try:
        ensure_initialized()
        products = get_blocked_products()
        return JSONResponse(content=products)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get blocked products: {str(e)}"},
        )


@router.post("/api/safety/blocked")
async def block_product(body: BlockProductRequest):
    """Add a product to the blocked list."""
    try:
        ensure_initialized()
        result = add_to_blocked_list(
            product_id=body.product_id,
            description=body.description,
            reason=body.reason,
        )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to block product: {str(e)}"},
        )


@router.delete("/api/safety/blocked/{product_id}")
async def unblock_product(product_id: str):
    """Remove a product from the blocked list."""
    try:
        ensure_initialized()
        result = remove_from_blocked_list(product_id)
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to unblock product: {str(e)}"},
        )
