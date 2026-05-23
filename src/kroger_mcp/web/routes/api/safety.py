"""Safety API endpoints — settings, approved products, and blocked products."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics.database import ensure_initialized
from kroger_mcp.analytics.ingredients import get_active_ingredients, get_all_ingredients
from kroger_mcp.analytics.safety import (
    add_to_blocked_list,
    add_to_safe_list,
    get_blocked_products,
    get_safe_products,
    get_safety_settings,
    remove_from_blocked_list,
    remove_from_safe_list,
    update_safety_settings,
)
from kroger_mcp.auth.dependencies import current_user_id

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SettingsRequest(BaseModel):
    filtering_enabled: bool | None = None
    block_mode: str | None = None


class ApproveProductRequest(BaseModel):
    product_id: str
    description: str | None = None
    brand: str | None = None
    reason: str | None = None


class BlockProductRequest(BaseModel):
    product_id: str
    description: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------


@router.get("/api/safety/settings")
async def get_settings(request: Request):
    """Get current safety filter settings for the authenticated user."""
    try:
        ensure_initialized()
        settings = get_safety_settings(user_id=current_user_id(request))
        return JSONResponse(content=settings)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get settings: {str(e)}"},
        )


@router.post("/api/safety/settings")
async def update_settings(request: Request, body: SettingsRequest):
    """Update safety filter settings for the authenticated user."""
    try:
        ensure_initialized()
        result = update_safety_settings(
            filtering_enabled=body.filtering_enabled,
            block_mode=body.block_mode,
            user_id=current_user_id(request),
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
async def list_approved(request: Request):
    """Get all safe-listed products for the authenticated user."""
    try:
        ensure_initialized()
        products = get_safe_products(user_id=current_user_id(request))
        return JSONResponse(content=products)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get safe products: {str(e)}"},
        )


@router.post("/api/safety/approved")
async def approve_product(request: Request, body: ApproveProductRequest):
    """Add a product to the authenticated user's safe list."""
    try:
        ensure_initialized()
        result = add_to_safe_list(
            product_id=body.product_id,
            description=body.description,
            brand=body.brand,
            reason=body.reason,
            user_id=current_user_id(request),
        )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to approve product: {str(e)}"},
        )


@router.delete("/api/safety/approved/{product_id}")
async def unapprove_product(request: Request, product_id: str):
    """Remove a product from the authenticated user's safe list."""
    try:
        ensure_initialized()
        result = remove_from_safe_list(product_id, user_id=current_user_id(request))
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
async def list_blocked(request: Request):
    """Get all blocked products for the authenticated user."""
    try:
        ensure_initialized()
        products = get_blocked_products(user_id=current_user_id(request))
        return JSONResponse(content=products)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get blocked products: {str(e)}"},
        )


@router.post("/api/safety/blocked")
async def block_product(request: Request, body: BlockProductRequest):
    """Add a product to the authenticated user's blocked list."""
    try:
        ensure_initialized()
        result = add_to_blocked_list(
            product_id=body.product_id,
            description=body.description,
            reason=body.reason,
            user_id=current_user_id(request),
        )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to block product: {str(e)}"},
        )


@router.delete("/api/safety/blocked/{product_id}")
async def unblock_product(request: Request, product_id: str):
    """Remove a product from the authenticated user's blocked list."""
    try:
        ensure_initialized()
        result = remove_from_blocked_list(product_id, user_id=current_user_id(request))
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to unblock product: {str(e)}"},
        )
