"""Cart API endpoints."""

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics.purchase_tracker import record_order
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.tools.cart_tools import (
    _add_item_to_local_cart,
    _load_cart_data,
    _load_order_history,
    _save_cart_data,
    _save_order_history,
)
from kroger_mcp.tools.shared import get_authenticated_client

logger = logging.getLogger(__name__)

router = APIRouter()


class CartAddBody(BaseModel):
    product_id: str
    quantity: int = 1
    modality: str = "PICKUP"
    description: str | None = None
    brand: str | None = None
    price: float | None = None


@router.post("/api/cart")
async def add_to_cart(body: CartAddBody, request: Request):
    """Add a single item to the local cart."""
    try:
        product_details: dict[str, Any] = {}
        if body.description:
            product_details["description"] = body.description
        if body.brand:
            product_details["brand"] = body.brand
        if body.price is not None:
            product_details["price"] = body.price
        _add_item_to_local_cart(
            product_id=body.product_id,
            quantity=body.quantity,
            modality=body.modality,
            product_details=product_details or None,
            user_id=current_user_id(request),
        )
        return JSONResponse(content={"success": True, "product_id": body.product_id})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to add to cart: {str(e)}"},
        )


@router.get("/api/cart")
async def get_cart():
    """Return the current cart contents."""
    try:
        cart_data = _load_cart_data()
        return JSONResponse(content=cart_data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to load cart: {str(e)}"},
        )


@router.delete("/api/cart/{product_id}")
async def remove_cart_item(product_id: str):
    """Remove a single item from the cart by product_id."""
    try:
        cart_data = _load_cart_data()
        current_cart = cart_data.get("current_cart", [])

        original_len = len(current_cart)
        cart_data["current_cart"] = [
            item for item in current_cart if item.get("product_id") != product_id
        ]

        if len(cart_data["current_cart"]) == original_len:
            return JSONResponse(
                status_code=404,
                content={"error": f"Item {product_id!r} not found in cart"},
            )

        cart_data["last_updated"] = datetime.now().isoformat()
        _save_cart_data(cart_data)
        return JSONResponse(content={"success": True, "removed": product_id})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to remove item: {str(e)}"},
        )


@router.delete("/api/cart")
async def clear_cart():
    """Clear all items from the current cart."""
    try:
        cart_data = _load_cart_data()
        cart_data["current_cart"] = []
        cart_data["last_updated"] = datetime.now().isoformat()
        _save_cart_data(cart_data)
        return JSONResponse(content={"success": True, "message": "Cart cleared"})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to clear cart: {str(e)}"},
        )


@router.post("/api/cart/mark-placed")
async def mark_order_placed(request: Request):
    """Push cart to Kroger API, record the order locally, and clear the cart."""
    try:
        cart_data = _load_cart_data()
        current_cart = cart_data.get("current_cart", [])

        if not current_cart:
            return JSONResponse(
                status_code=400,
                content={"error": "Cart is empty — nothing to place"},
            )

        # Push to the real Kroger cart first
        kroger_cart_updated = False
        kroger_warning = None
        kroger_failed_items: list = []
        try:
            client = await asyncio.to_thread(get_authenticated_client, current_user_id(request))
            kroger_items = [
                {
                    "upc": item["product_id"],
                    "quantity": item.get("quantity", 1),
                    "modality": item.get("modality", "PICKUP"),
                }
                for item in current_cart
            ]
            # Kroger API supports up to 50 items per call — chunk if needed
            chunk_size = 50
            for i in range(0, len(kroger_items), chunk_size):
                chunk = kroger_items[i : i + chunk_size]
                try:
                    await asyncio.to_thread(client.cart.add_to_cart, chunk)
                except Exception as chunk_err:
                    chunk_err_str = str(chunk_err)
                    is_400 = "400" in chunk_err_str or "Bad Request" in chunk_err_str
                    if is_400 and len(chunk) > 1:
                        # Fall back to per-item adds for this chunk
                        print(f"Chunk add failed (400), retrying {len(chunk)} items one at a time")
                        for kroger_item in chunk:
                            try:
                                await asyncio.to_thread(client.cart.add_to_cart, [kroger_item])
                            except Exception as item_err:
                                kroger_failed_items.append(
                                    {
                                        "upc": kroger_item["upc"],
                                        "error": str(item_err),
                                    }
                                )
                    else:
                        raise
            kroger_cart_updated = True
        except Exception as kroger_err:
            kroger_warning = str(kroger_err)
            logger.warning("Could not push to Kroger cart API: %s", kroger_err)

        # Record the order in purchase analytics
        try:
            record_order(current_cart, user_id=current_user_id(request))
        except Exception as record_err:
            logger.warning("Could not record order analytics: %s", record_err)

        # Save to local order history
        try:
            history = _load_order_history()
            history.append(
                {
                    "items": current_cart,
                    "placed_at": datetime.now().isoformat(),
                    "item_count": len(current_cart),
                    "kroger_cart_updated": kroger_cart_updated,
                }
            )
            _save_order_history(history)
        except Exception as hist_err:
            logger.warning("Could not save order history: %s", hist_err)

        # Restock pantry
        try:
            from ....analytics.pantry import restock_item

            for item in current_cart:
                pid = item.get("product_id")
                if pid:
                    try:
                        restock_item(product_id=pid, level=100)
                    except Exception:
                        pass
        except Exception:
            pass

        # Clear the local cart
        cart_data["current_cart"] = []
        cart_data["last_updated"] = datetime.now().isoformat()
        _save_cart_data(cart_data)

        items_sent = len(current_cart) - len(kroger_failed_items)
        result = {
            "success": True,
            "message": f"Order placed with {len(current_cart)} items. Cart cleared.",
            "item_count": len(current_cart),
            "kroger_cart_updated": kroger_cart_updated,
            "kroger_items_sent": items_sent,
        }
        if kroger_failed_items:
            result["kroger_items_failed"] = len(kroger_failed_items)
            result["kroger_failed_upcs"] = [f["upc"] for f in kroger_failed_items]
            result["kroger_warning"] = (
                f"{len(kroger_failed_items)} item(s) rejected by Kroger API "
                "(invalid product ID or not available at this location). "
                f"Failed UPCs: {', '.join(f['upc'] for f in kroger_failed_items)}"
            )
        if kroger_warning:
            result["kroger_warning"] = f"Could not push to Kroger API: {kroger_warning}"

        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to mark order placed: {str(e)}"},
        )


@router.get("/api/cart/history")
async def get_cart_history():
    """Return the last 20 order history entries."""
    try:
        history = _load_order_history()
        return JSONResponse(content={"history": history[-20:] if history else []})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to load order history: {str(e)}"},
        )
