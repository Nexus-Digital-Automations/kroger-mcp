"""Cart API endpoints."""
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from kroger_mcp.tools.cart_tools import (
    _load_cart_data,
    _save_cart_data,
    _load_order_history,
    _save_order_history,
)
from kroger_mcp.analytics.purchase_tracker import record_order

router = APIRouter()


@router.get('/api/cart')
async def get_cart():
    """Return the current cart contents."""
    try:
        cart_data = _load_cart_data()
        return JSONResponse(content=cart_data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to load cart: {str(e)}'},
        )


@router.delete('/api/cart/{product_id}')
async def remove_cart_item(product_id: str):
    """Remove a single item from the cart by product_id."""
    try:
        cart_data = _load_cart_data()
        current_cart = cart_data.get('current_cart', [])

        original_len = len(current_cart)
        cart_data['current_cart'] = [
            item for item in current_cart
            if item.get('product_id') != product_id
        ]

        if len(cart_data['current_cart']) == original_len:
            return JSONResponse(
                status_code=404,
                content={'error': f'Item {product_id!r} not found in cart'},
            )

        cart_data['last_updated'] = datetime.now().isoformat()
        _save_cart_data(cart_data)
        return JSONResponse(content={'success': True, 'removed': product_id})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to remove item: {str(e)}'},
        )


@router.delete('/api/cart')
async def clear_cart():
    """Clear all items from the current cart."""
    try:
        cart_data = _load_cart_data()
        cart_data['current_cart'] = []
        cart_data['last_updated'] = datetime.now().isoformat()
        _save_cart_data(cart_data)
        return JSONResponse(content={'success': True, 'message': 'Cart cleared'})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to clear cart: {str(e)}'},
        )


@router.post('/api/cart/mark-placed')
async def mark_order_placed():
    """Mark the current cart as an order placed, then clear it."""
    try:
        cart_data = _load_cart_data()
        current_cart = cart_data.get('current_cart', [])

        if not current_cart:
            return JSONResponse(
                status_code=400,
                content={'error': 'Cart is empty — nothing to place'},
            )

        # Record the order in purchase analytics
        try:
            record_order(current_cart)
        except Exception as record_err:
            # Log but don't fail the whole operation
            print(f'Warning: could not record order analytics: {record_err}')

        # Save to local order history as well
        try:
            history = _load_order_history()
            history.append({
                'items': current_cart,
                'placed_at': datetime.now().isoformat(),
                'item_count': len(current_cart),
            })
            _save_order_history(history)
        except Exception as hist_err:
            print(f'Warning: could not save order history: {hist_err}')

        # Clear the cart
        cart_data['current_cart'] = []
        cart_data['last_updated'] = datetime.now().isoformat()
        _save_cart_data(cart_data)

        return JSONResponse(content={
            'success': True,
            'message': f'Order placed with {len(current_cart)} items. Cart cleared.',
            'item_count': len(current_cart),
        })
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to mark order placed: {str(e)}'},
        )


@router.get('/api/cart/history')
async def get_cart_history():
    """Return the last 20 order history entries."""
    try:
        history = _load_order_history()
        return JSONResponse(content={'history': history[-20:] if history else []})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to load order history: {str(e)}'},
        )
