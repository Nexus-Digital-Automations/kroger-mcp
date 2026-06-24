"""Deals API endpoints — find sales, manage watchlist, price history."""

import asyncio
import functools
import json
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_connection,
    get_db_cursor,
)
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.cache import cache_read_through, get_redis
from kroger_mcp.tools.shared import (
    get_client_credentials_client,
    get_preferred_location_id,
    kroger_cache_key,
)

# Shared 1h TTL for on-sale product searches (deals move slowly within the hour).
_DEAL_SEARCH_TTL = 3600

router = APIRouter()


# ---------------------------------------------------------------------------
# Internal helper — same logic as products.py but import-safe
# ---------------------------------------------------------------------------


def _extract_deal_product(item) -> dict | None:
    """Extract and normalise a product item from the Kroger API response."""
    try:
        if hasattr(item, "__dict__") and not isinstance(item, dict):
            item = vars(item)

        pid = item.get("productId") or item.get("upc", "")
        desc = item.get("description", "")
        brand = item.get("brand", "")

        items_data = item.get("items", [{}])
        first_item = items_data[0] if items_data else {}
        if not isinstance(first_item, dict):
            try:
                first_item = vars(first_item)
            except Exception:
                first_item = {}

        price_data = first_item.get("price", {})
        if not isinstance(price_data, dict):
            try:
                price_data = vars(price_data)
            except Exception:
                price_data = {}

        regular = price_data.get("regular")
        promo = price_data.get("promo")

        on_sale = promo is not None and promo > 0 and (regular is None or promo < regular)
        savings_pct = round((1 - promo / regular) * 100, 1) if on_sale and regular and promo else 0

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


# ---------------------------------------------------------------------------
# GET /api/deals/auto  — parallel scan of broad categories for best deals
# ---------------------------------------------------------------------------

_AUTO_CATEGORIES = [
    "produce",
    "meat",
    "seafood",
    "dairy",
    "frozen",
    "snacks",
    "beverages",
    "bread",
    "pantry",
    "deli",
]


@router.get("/api/deals/auto")
async def auto_deals(request: Request, min_savings: float = 5):
    """Scan multiple grocery categories in parallel and return top deals."""
    try:
        user_id = current_user_id(request)
        client = get_client_credentials_client(user_id)
        location_id = get_preferred_location_id(user_id=user_id) or "03400014"
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    # Cache the whole scan: ~5 Kroger searches per click, and deals barely move
    # within 15 min. Best-effort — degrades to a live scan when Redis is down.
    cache_key = f"deals:auto:{location_id}:{min_savings:g}"
    redis = get_redis()
    if redis is not None:
        try:
            cached = redis.get(cache_key)
            if cached is not None:
                return JSONResponse(content=json.loads(cached))
        except Exception:
            pass

    async def _search(term: str) -> list:
        try:
            result = await asyncio.to_thread(
                cache_read_through,
                kroger_cache_key(
                    client, "product_search", term=term, location=location_id, limit=50
                ),
                _DEAL_SEARCH_TTL,
                functools.partial(
                    client.product.search_products,
                    term=term,
                    location_id=location_id,
                    limit=50,
                ),
            )
            if isinstance(result, dict):
                return result.get("data", []) or []
            if hasattr(result, "data"):
                return result.data or []
            if isinstance(result, list):
                return result
        except Exception:
            pass
        return []

    batches = await asyncio.gather(*[_search(t) for t in _AUTO_CATEGORIES])

    seen: dict = {}
    for batch in batches:
        for item in batch:
            extracted = _extract_deal_product(item)
            if (
                extracted
                and extracted.get("product_id")
                and extracted.get("on_sale")
                and extracted.get("savings_percent", 0) >= min_savings
                and extracted["product_id"] not in seen
            ):
                seen[extracted["product_id"]] = extracted

    deals = sorted(seen.values(), key=lambda x: x.get("savings_percent", 0), reverse=True)

    try:
        ensure_initialized()
        from kroger_mcp.analytics.deals import record_price_observation

        for p in deals[:100]:
            record_price_observation(
                product_id=p["product_id"],
                regular_price=p.get("regular_price"),
                sale_price=p.get("sale_price"),
                location_id=location_id,
                source="web_auto",
            )
    except Exception:
        pass

    # Enrich with safety scores (best-effort)
    try:
        from kroger_mcp.analytics.safety import check_products_safety_batch

        statuses = check_products_safety_batch(deals)
        for deal, status in zip(deals, statuses, strict=False):
            d = status.to_dict()
            deal["safety_score"] = d.get("safety_score")
            deal["safety_grade"] = d.get("safety_grade")
            deal["safety_status"] = d.get("safety_status")
            deal["flagged_ingredients"] = d.get("flagged_ingredients", [])
            deal["positive_attributes"] = d.get("positive_attributes", [])
    except Exception:
        pass

    # Cache write (miss path only). record_price_observation above therefore runs
    # at most once per TTL — fine, the scan cadence stays >= every 15 min.
    if redis is not None:
        try:
            redis.set(cache_key, json.dumps(deals), ex=900)
        except Exception:
            pass

    return JSONResponse(content=deals)


# ---------------------------------------------------------------------------
# GET /api/deals/find
# ---------------------------------------------------------------------------


@router.get("/api/deals/find")
async def find_deals(
    request: Request,
    q: str = "",
    category: str = "",
    min_savings: float = 10,
):
    """Search for products currently on sale, filtered by minimum savings %."""
    search_term = q.strip() or category.strip() or "sale"

    try:
        user_id = current_user_id(request)
        client = get_client_credentials_client(user_id)
        location_id = get_preferred_location_id(user_id=user_id) or "03400014"
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    try:
        result = await asyncio.to_thread(
            cache_read_through,
            kroger_cache_key(
                client, "product_search", term=search_term, location=location_id, limit=50
            ),
            _DEAL_SEARCH_TTL,
            functools.partial(
                client.product.search_products,
                term=search_term,
                location_id=location_id,
                limit=50,
            ),
        )
        if isinstance(result, dict):
            raw_products = result.get("data", []) or []
        elif hasattr(result, "data"):
            raw_products = result.data or []
        elif isinstance(result, list):
            raw_products = result
        else:
            raw_products = []
    except Exception:
        raw_products = []

    deals = [
        p
        for item in raw_products
        if (p := _extract_deal_product(item))
        and p.get("product_id")
        and p.get("on_sale")
        and p.get("savings_percent", 0) >= min_savings
    ]

    # Record price observations (best-effort)
    try:
        ensure_initialized()
        from kroger_mcp.analytics.deals import record_price_observation

        for p in deals:
            record_price_observation(
                product_id=p["product_id"],
                regular_price=p.get("regular_price"),
                sale_price=p.get("sale_price"),
                location_id=location_id,
                source="web_deals",
            )
    except Exception:
        pass

    # Enrich with safety scores (best-effort)
    try:
        from kroger_mcp.analytics.safety import check_products_safety_batch

        statuses = check_products_safety_batch(deals)
        for deal, status in zip(deals, statuses, strict=False):
            d = status.to_dict()
            deal["safety_score"] = d.get("safety_score")
            deal["safety_grade"] = d.get("safety_grade")
            deal["safety_status"] = d.get("safety_status")
            deal["flagged_ingredients"] = d.get("flagged_ingredients", [])
            deal["positive_attributes"] = d.get("positive_attributes", [])
    except Exception:
        pass

    return JSONResponse(content=deals)


# ---------------------------------------------------------------------------
# GET /api/deals/watchlist
# ---------------------------------------------------------------------------


@router.get("/api/deals/watchlist")
async def get_watchlist(request: Request):
    """Return the authenticated user's deal watchlist."""
    try:
        ensure_initialized()
        user_id = current_user_id(request)
        conn = get_db_connection()
        cursor = conn.execute(
            "SELECT * FROM deal_watchlist WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return JSONResponse(content=rows)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to load watchlist: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# POST /api/deals/watchlist
# ---------------------------------------------------------------------------


class WatchlistAddBody(BaseModel):
    product_id: str
    description: str = ""
    target_price: float | None = None


@router.post("/api/deals/watchlist")
async def add_to_watchlist(body: WatchlistAddBody, request: Request):
    """Add a product to the authenticated user's deal watchlist."""
    try:
        ensure_initialized()
        user_id = current_user_id(request)
        now = datetime.now().isoformat()
        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT OR IGNORE INTO products (product_id, description) VALUES (?, ?)",
                (body.product_id, body.description or None),
            )
            cursor.execute(
                """
                INSERT INTO deal_watchlist
                    (user_id, product_id, description, target_price, added_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, product_id) DO UPDATE SET
                    description = excluded.description,
                    target_price = excluded.target_price
                """,
                (user_id, body.product_id, body.description, body.target_price, now),
            )
        return JSONResponse(
            content={
                "success": True,
                "product_id": body.product_id,
            }
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to add to watchlist: {str(e)}"},
        )


@router.delete("/api/deals/watchlist/{product_id}")
async def remove_from_watchlist(product_id: str, request: Request):
    """Remove a product from the authenticated user's deal watchlist."""
    try:
        ensure_initialized()
        user_id = current_user_id(request)
        with get_db_cursor() as cursor:
            cursor.execute(
                "DELETE FROM deal_watchlist WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            )
        return JSONResponse(content={"success": True, "removed": product_id})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to remove from watchlist: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# DELETE /api/deals/watchlist/{product_id}
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /api/deals/price-history/{product_id}
# ---------------------------------------------------------------------------


@router.get("/api/deals/price-history/{product_id}")
async def get_price_history(product_id: str, days: int = 30):
    """Return price statistics and history for a given product."""
    try:
        ensure_initialized()
        from kroger_mcp.analytics.deals import get_price_statistics

        location_id = get_preferred_location_id() or "03400014"
        stats = get_price_statistics(
            product_id=product_id,
            days=days,
            location_id=location_id,
        )

        # Also pull the raw observations for charting
        conn = get_db_connection()
        cursor = conn.execute(
            """
            SELECT regular_price, sale_price, on_sale, savings_percent,
                   observed_at
            FROM price_history
            WHERE product_id = ?
            ORDER BY observed_at DESC
            LIMIT 60
            """,
            (product_id,),
        )
        observations = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return JSONResponse(
            content={
                "product_id": product_id,
                "statistics": stats,
                "observations": observations,
            }
        )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get price history: {str(e)}"},
        )
