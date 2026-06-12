"""Products API endpoints — search, single-product detail, and cart add."""

import asyncio
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics.database import run_in_thread
from kroger_mcp.analytics.ingredient_links import (
    best_guess,
    get_canonical_name,
    suggest_products_for_ingredient,
)
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.cache import cache_read_through
from kroger_mcp.tools.shared import (
    get_authenticated_client,
    get_client_credentials_client,
    get_preferred_location_id,
    kroger_cache_key,
)
from kroger_mcp.web.routes.api._product_extract import _extract_product

# Public-read cache TTLs (seconds). Product prices drift slowly; a 1h window
# spares the shared rate bucket without showing stale prices.
_PRODUCT_SEARCH_TTL = 3600
_PRODUCT_DETAIL_TTL = 3600

router = APIRouter()
logger = logging.getLogger("kroger_mcp.web.products")


def _record_observations_bg(observations: list[dict]) -> None:
    """Persist price observations on a background thread (best-effort).

    Runs off the request path via ``asyncio.to_thread``. Swallows and logs
    any failure so a transient DB error never surfaces to the client — this
    mirrors the old per-item ``except Exception: pass`` behavior.
    """
    try:
        from kroger_mcp.analytics.database import ensure_initialized
        from kroger_mcp.analytics.deals import record_price_observations

        ensure_initialized()
        record_price_observations(observations)
    except Exception:
        logger.exception(
            "background price observation write failed (count=%d)",
            len(observations),
        )


@router.get("/api/products/search")
async def search_products(
    request: Request,
    q: str = "",
    limit: int = 20,
    category: str = "",
):
    """Search Kroger products and return normalised JSON."""
    search_term = q.strip() or category.strip()
    # Kroger API rejects long terms and special chars — strip apostrophes etc. and truncate
    search_term = re.sub(r"[^\w\s-]", "", search_term).strip()
    if len(search_term) > 50:
        search_term = search_term[:50].rsplit(" ", 1)[0].strip()
    if not search_term:
        return JSONResponse(
            status_code=400,
            content={"error": "Provide a search term or category"},
        )

    try:
        user_id = current_user_id(request)
        # All sync work (Kroger HTTP on cache miss, Redis, safety batch) runs
        # off the event loop: a slow Kroger response must not stall every
        # other request on the single prod worker.
        products, observations = await run_in_thread(
            _search_products_payload, user_id, search_term, min(limit, 50)
        )

        # Record price observations off the request path (best-effort):
        # fire-and-forget a single batched insert on a worker thread.
        if observations:
            asyncio.create_task(
                asyncio.to_thread(_record_observations_bg, observations)
            )

        return JSONResponse(content=products)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Product search failed: {str(e)}"},
        )


def _search_products_payload(
    user_id: str | None, search_term: str, capped_limit: int
) -> tuple[list[dict], list[dict]]:
    """Sync body of /api/products/search, run via run_in_thread."""
    client = get_client_credentials_client(user_id)
    location_id = get_preferred_location_id(user_id=user_id) or "03400014"

    cache_key = kroger_cache_key(
        client,
        "product_search",
        term=search_term,
        location=location_id,
        limit=capped_limit,
    )
    result = cache_read_through(
        cache_key,
        _PRODUCT_SEARCH_TTL,
        lambda: client.product.search_products(
            term=search_term,
            location_id=location_id,
            limit=capped_limit,
        ),
    )

    # Unwrap the result — search_products returns a dict with 'data' key
    raw_products: list = []
    if isinstance(result, dict):
        raw_products = result.get("data", []) or []
    elif hasattr(result, "data"):
        raw_products = result.data or []
    elif isinstance(result, list):
        raw_products = result

    products = []
    for item in raw_products:
        extracted = _extract_product(item)
        if extracted and extracted.get("product_id"):
            products.append(extracted)

    observations = [
        {
            "product_id": p["product_id"],
            "regular_price": p.get("regular_price"),
            "sale_price": p.get("sale_price"),
            "location_id": location_id,
            "source": "web_search",
        }
        for p in products
        if p.get("product_id")
    ]

    # Enrich with safety scores (best-effort)
    try:
        from kroger_mcp.analytics.safety import check_products_safety_batch

        statuses = check_products_safety_batch(products)
        for product, status in zip(products, statuses, strict=False):
            d = status.to_dict()
            product["safety_score"] = d.get("safety_score")
            product["safety_grade"] = d.get("safety_grade")
            product["safety_status"] = d.get("safety_status")
            product["flagged_ingredients"] = d.get("flagged_ingredients", [])
            product["positive_attributes"] = d.get("positive_attributes", [])
    except Exception:
        pass

    return products, observations


@router.get("/api/ingredients/suggest")
async def suggest_ingredient_products(request: Request, name: str = "", limit: int = 6):
    """The calling account's "usual" products for a typed ingredient name.

    Powers the popover's smart auto-link: a learned canonical name, a
    pre-selectable best guess, and a ranked "your usuals" list — all scoped to
    this account (recipes stay global; the memory is per-user). Suggestion rows
    carry product_id + description + reason only; the popover lazy-loads price /
    safety / image via /api/products/{id} on expand, keeping this path cheap.

    Cold start (no history) returns empty suggestions with best_guess=null and
    HTTP 200. 401 when unauthenticated.
    """
    term = (name or "").strip()
    if not term:
        return JSONResponse(
            status_code=400, content={"error": "Provide an ingredient name"}
        )

    user_id = current_user_id(request)
    try:
        suggestions = suggest_products_for_ingredient(user_id, term, limit=limit)
        canonical = get_canonical_name(user_id, term)
        guess = best_guess(suggestions)
    except Exception as exc:
        # Suggestions are an enhancement; surface a clean empty result, not 500.
        logger.warning("ingredient_suggest_failed name=%r", term, exc_info=True)
        return JSONResponse(
            status_code=200,
            content={
                "canonical_name": None,
                "canonical_confidence": None,
                "best_guess": None,
                "suggestions": [],
                "error": str(exc),
            },
        )

    return JSONResponse(
        content={
            "canonical_name": canonical["canonical_name"] if canonical else None,
            "canonical_confidence": canonical["confidence"] if canonical else None,
            "best_guess": guess,
            "suggestions": suggestions,
        }
    )


@router.get("/api/products/{product_id}")
async def get_product_detail(request: Request, product_id: str):
    """Return one product with safety enrichment and (when Kroger provides
    them) ingredient list and nutrition. Source of truth for the comparison
    panel in the recipe-linking UI.

    Failure modes: 400 on blank id, 404 when Kroger has no such product,
    500 on upstream errors.
    """
    pid = (product_id or "").strip()
    if not pid:
        return JSONResponse(status_code=400, content={"error": "Missing product_id"})

    try:
        user_id = current_user_id(request)
        # Off-loop for the same reason as search: a slow Kroger response must
        # not stall the single worker.
        product = await run_in_thread(_product_detail_payload, user_id, pid)
        if product is None:
            return JSONResponse(status_code=404, content={"error": "Product not found"})
        return JSONResponse(content=product)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Product lookup failed: {str(e)}"},
        )


def _product_detail_payload(user_id: str | None, pid: str) -> dict | None:
    """Sync body of /api/products/{id}, run via run_in_thread. None = 404."""
    client = get_client_credentials_client(user_id)
    location_id = get_preferred_location_id(user_id=user_id) or "03400014"
    cache_key = kroger_cache_key(
        client, "product_detail", pid=pid, location=location_id
    )
    raw = cache_read_through(
        cache_key,
        _PRODUCT_DETAIL_TTL,
        lambda: client.product.get_product(product_id=pid, location_id=location_id),
    )

    # Kroger SDK returns {"data": {...}} or an object exposing .data
    record = None
    if isinstance(raw, dict):
        record = raw.get("data")
    elif hasattr(raw, "data"):
        record = raw.data
    if not record:
        return None

    product = _extract_product(record)
    if not product or not product.get("product_id"):
        return None

    # Pass through fields only the single-product endpoint tends to populate.
    record_dict = vars(record) if not isinstance(record, dict) else record
    ingredients_text = record_dict.get("ingredients") or record_dict.get("ingredientsText")
    if ingredients_text:
        product["ingredients_list"] = ingredients_text
    nutrition = record_dict.get("nutrition") or record_dict.get("nutritionFacts")
    if nutrition:
        product["nutrition"] = nutrition

    try:
        from kroger_mcp.analytics.safety import check_products_safety_batch

        statuses = check_products_safety_batch([product])
        if statuses:
            d = statuses[0].to_dict()
            product["safety_score"] = d.get("safety_score")
            product["safety_grade"] = d.get("safety_grade")
            product["safety_status"] = d.get("safety_status")
            product["flagged_ingredients"] = d.get("flagged_ingredients", [])
            product["positive_attributes"] = d.get("positive_attributes", [])
    except Exception:
        # Enrichment is best-effort — leave grade/score absent rather than fail the call.
        pass

    return product


class AddToCartBody(BaseModel):
    quantity: int = 1
    modality: str = "PICKUP"
    description: str = ""
    price: float = 0.0


@router.post("/api/products/{product_id}/add-to-cart")
async def add_product_to_cart(product_id: str, body: AddToCartBody, request: Request):
    """Add a single product to the Kroger cart and local cart tracking."""
    try:
        # Off-loop like cart.py / shopping_list.py: the Kroger cart POST (and
        # its retry backoff sleeps) must not block the single worker.
        client = await asyncio.to_thread(
            get_authenticated_client, current_user_id(request)
        )
        await asyncio.to_thread(
            client.cart.add_to_cart,
            items=[
                {
                    "upc": product_id,
                    "quantity": body.quantity,
                    "modality": body.modality,
                }
            ],
        )

        # Mirror in local cart tracking (best-effort)
        try:
            from kroger_mcp.tools.cart_tools import _add_item_to_local_cart

            await asyncio.to_thread(
                _add_item_to_local_cart,
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

        return JSONResponse(
            content={
                "success": True,
                "product_id": product_id,
                "quantity": body.quantity,
                "modality": body.modality,
            }
        )

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
