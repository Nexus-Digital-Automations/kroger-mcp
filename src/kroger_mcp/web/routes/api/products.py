"""Products API endpoints — search and cart add."""
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.tools.shared import (
    get_client_credentials_client,
    get_authenticated_client,
    get_preferred_location_id,
)

router = APIRouter()


def _extract_product(item) -> Optional[dict]:
    """Normalise a Kroger API product object/dict into a flat dict."""
    try:
        # Handle both attribute-style and dict-style API responses
        if hasattr(item, '__dict__') and not isinstance(item, dict):
            item = vars(item)

        pid = item.get('productId') or item.get('upc', '')
        desc = item.get('description', '')
        brand = item.get('brand', '')

        items_data = item.get('items', [{}])
        first_item = items_data[0] if items_data else {}

        if not isinstance(first_item, dict):
            # Object-style item sub-entry
            try:
                first_item = vars(first_item)
            except Exception:
                first_item = {}

        price_data = first_item.get('price', {})
        if not isinstance(price_data, dict):
            try:
                price_data = vars(price_data)
            except Exception:
                price_data = {}

        regular = price_data.get('regular')
        promo = price_data.get('promo')

        # Treat promo as sale only when it is a positive number below regular
        on_sale = (
            promo is not None
            and promo > 0
            and (regular is None or promo < regular)
        )
        savings_pct = (
            round((1 - promo / regular) * 100, 1)
            if on_sale and regular and promo
            else 0
        )

        return {
            "product_id": pid,
            "description": desc,
            "brand": brand,
            "regular_price": regular,
            "sale_price": promo if on_sale else None,
            "on_sale": on_sale,
            "savings_percent": savings_pct,
        }
    except Exception:
        return None


@router.get('/api/products/search')
async def search_products(
    q: str = '',
    limit: int = 20,
    category: str = '',
):
    """Search Kroger products and return normalised JSON."""
    search_term = q.strip() or category.strip()
    if not search_term:
        return JSONResponse(
            status_code=400,
            content={"error": "Provide a search term or category"},
        )

    try:
        client = get_client_credentials_client()
        location_id = get_preferred_location_id() or "03400014"

        result = client.product.search_products(
            term=search_term,
            location_id=location_id,
            limit=min(limit, 50),
        )

        # Unwrap the result — search_products returns a dict with 'data' key
        raw_products = []
        if isinstance(result, dict):
            raw_products = result.get('data', []) or []
        elif hasattr(result, 'data'):
            raw_products = result.data or []
        elif isinstance(result, list):
            raw_products = result

        products = []
        for item in raw_products:
            extracted = _extract_product(item)
            if extracted and extracted.get('product_id'):
                products.append(extracted)

        # Record price observations in the background (best-effort)
        try:
            from kroger_mcp.analytics.deals import record_price_observation
            from kroger_mcp.analytics.database import ensure_initialized
            ensure_initialized()
            for p in products:
                if p.get('product_id'):
                    record_price_observation(
                        product_id=p['product_id'],
                        regular_price=p.get('regular_price'),
                        sale_price=p.get('sale_price'),
                        location_id=location_id,
                        source='web_search',
                    )
        except Exception:
            pass

        return JSONResponse(content=products)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Product search failed: {str(e)}"},
        )


class AddToCartBody(BaseModel):
    quantity: int = 1
    modality: str = "PICKUP"
    description: str = ""
    price: float = 0.0


@router.post('/api/products/{product_id}/add-to-cart')
async def add_product_to_cart(product_id: str, body: AddToCartBody):
    """Add a single product to the Kroger cart and local cart tracking."""
    try:
        client = get_authenticated_client()
        client.cart.add_to_cart(items=[{
            "upc": product_id,
            "quantity": body.quantity,
            "modality": body.modality,
        }])

        # Mirror in local cart tracking (best-effort)
        try:
            from kroger_mcp.tools.cart_tools import _add_item_to_local_cart
            _add_item_to_local_cart(
                product_id=product_id,
                quantity=body.quantity,
                modality=body.modality,
                product_details={
                    "description": body.description,
                    "price": body.price,
                },
            )
        except Exception:
            pass

        return JSONResponse(content={
            "success": True,
            "product_id": product_id,
            "quantity": body.quantity,
            "modality": body.modality,
        })

    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err or "Authentication" in err:
            return JSONResponse(
                status_code=401,
                content={"error": "Not authenticated. Please log in via Claude first."},
            )
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to add to cart: {err}"},
        )
