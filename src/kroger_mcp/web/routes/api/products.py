"""Products API endpoints — search, single-product detail, and cart add."""

import asyncio
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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


# Pre-compiled. The size grammar Kroger returns is short and stable
# ("16 oz", "1.5 lb", "12 ct", "6 x 12 oz", "1 gal", "750 ml").
_SIZE_RE = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)\s*[x×]\s*)?"  # optional pack multiplier
    r"(\d+(?:\.\d+)?)\s*"                  # quantity
    r"(fl\s*oz|oz|lb|lbs|pound|pounds|"    # weight / volume / count units
    r"gal|qt|pt|ml|l|ct|count|each|ea|pk|pack)\b",
    re.IGNORECASE,
)

# Normalise everything to one of three canonical units the UI can display.
# Weights → per oz, volumes → per fl oz, counts → per each.
_UNIT_TO_CANONICAL: dict[str, tuple[str, float]] = {
    "oz": ("oz", 1.0),
    "lb": ("oz", 16.0),
    "lbs": ("oz", 16.0),
    "pound": ("oz", 16.0),
    "pounds": ("oz", 16.0),
    "fl oz": ("fl oz", 1.0),
    "floz": ("fl oz", 1.0),
    "gal": ("fl oz", 128.0),
    "qt": ("fl oz", 32.0),
    "pt": ("fl oz", 16.0),
    "ml": ("fl oz", 1.0 / 29.5735),
    "l": ("fl oz", 33.814),
    "ct": ("each", 1.0),
    "count": ("each", 1.0),
    "each": ("each", 1.0),
    "ea": ("each", 1.0),
    "pk": ("each", 1.0),
    "pack": ("each", 1.0),
}


def _compute_price_per_unit(size: str | None, price: float | None) -> dict | None:
    """Return ``{amount, unit, label}`` for a parseable size + price, else None.

    Failure modes: unparseable size strings, missing price, zero/negative
    quantities. All return ``None`` so the UI cleanly omits the line.
    """
    if not size or price is None or price <= 0:
        return None
    match = _SIZE_RE.match(size)
    if not match:
        return None
    pack_str, qty_str, unit_raw = match.groups()
    unit_key = re.sub(r"\s+", " ", unit_raw.strip().lower())
    if unit_key == "fl oz":
        unit_key = "fl oz"
    canonical = _UNIT_TO_CANONICAL.get(unit_key)
    if canonical is None:
        return None
    canonical_unit, factor = canonical
    try:
        total_qty = float(qty_str) * factor * (float(pack_str) if pack_str else 1.0)
    except ValueError:
        return None
    if total_qty <= 0:
        return None
    per = price / total_qty
    return {
        "amount": round(per, 4),
        "unit": canonical_unit,
        "label": f"${per:.2f}/{canonical_unit}" if per >= 0.10 else f"${per * 100:.1f}¢/{canonical_unit}",
    }


def _pick_image_url(images: list | None) -> str | None:
    """Choose the front-perspective, medium-sized image URL when available."""
    if not images:
        return None
    candidates: list[tuple[int, str]] = []
    size_priority = {"medium": 0, "large": 1, "small": 2, "xlarge": 3, "thumbnail": 4}
    for img in images:
        if not isinstance(img, dict):
            continue
        perspective = (img.get("perspective") or "").lower()
        if perspective and perspective != "front":
            continue
        for sz in img.get("sizes", []) or []:
            if not isinstance(sz, dict):
                continue
            url = sz.get("url")
            if not url:
                continue
            rank = size_priority.get((sz.get("size") or "").lower(), 99)
            candidates.append((rank, url))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def _extract_product(item) -> dict | None:
    """Normalise a Kroger API product object/dict into a flat dict."""
    try:
        # Handle both attribute-style and dict-style API responses
        if hasattr(item, "__dict__") and not isinstance(item, dict):
            item = vars(item)

        pid = item.get("productId") or item.get("upc", "")
        desc = item.get("description", "")
        brand = item.get("brand", "")

        items_data = item.get("items", [{}])
        first_item = items_data[0] if items_data else {}

        if not isinstance(first_item, dict):
            # Object-style item sub-entry
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

        # Treat promo as sale only when it is a positive number below regular
        on_sale = promo is not None and promo > 0 and (regular is None or promo < regular)
        savings_pct = round((1 - promo / regular) * 100, 1) if on_sale and regular and promo else 0

        size = first_item.get("size")
        image_url = _pick_image_url(item.get("images"))
        effective_price = promo if on_sale else regular
        price_per_unit = _compute_price_per_unit(size, effective_price)
        # Kroger sometimes provides its own per-unit estimate without a labelled
        # unit; only use it as a fallback so the UI still shows *something*.
        if price_per_unit is None:
            estimate = price_data.get("regularPerUnitEstimate")
            if isinstance(estimate, int | float) and estimate > 0:
                price_per_unit = {
                    "amount": round(float(estimate), 4),
                    "unit": "unit",
                    "label": f"${float(estimate):.2f}/unit",
                }

        aisle_raw = item.get("aisleLocations") or []
        aisle = None
        if aisle_raw and isinstance(aisle_raw[0], dict):
            aisle = aisle_raw[0].get("description") or aisle_raw[0].get("number")

        return {
            "product_id": pid,
            "description": desc,
            "brand": brand,
            "regular_price": regular,
            "sale_price": promo if on_sale else None,
            "on_sale": on_sale,
            "savings_percent": savings_pct,
            "size": size,
            "image_url": image_url,
            "price_per_unit": price_per_unit,
            "aisle": aisle,
            "country_origin": item.get("countryOrigin"),
        }
    except Exception:
        return None


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
        client = get_client_credentials_client(user_id)
        location_id = get_preferred_location_id(user_id=user_id) or "03400014"
        capped_limit = min(limit, 50)

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

        # Record price observations off the request path (best-effort).
        # Collect every observation, then fire-and-forget a single batched
        # insert on a worker thread so the response is not blocked on N DB
        # writes. Identical data is recorded, just batched + asynchronous.
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
        if observations:
            asyncio.create_task(
                asyncio.to_thread(_record_observations_bg, observations)
            )

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

        return JSONResponse(content=products)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Product search failed: {str(e)}"},
        )


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
            return JSONResponse(status_code=404, content={"error": "Product not found"})

        product = _extract_product(record)
        if not product or not product.get("product_id"):
            return JSONResponse(status_code=404, content={"error": "Product not found"})

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

        return JSONResponse(content=product)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Product lookup failed: {str(e)}"},
        )


class AddToCartBody(BaseModel):
    quantity: int = 1
    modality: str = "PICKUP"
    description: str = ""
    price: float = 0.0


@router.post("/api/products/{product_id}/add-to-cart")
async def add_product_to_cart(product_id: str, body: AddToCartBody, request: Request):
    """Add a single product to the Kroger cart and local cart tracking."""
    try:
        client = get_authenticated_client(current_user_id(request))
        client.cart.add_to_cart(
            items=[
                {
                    "upc": product_id,
                    "quantity": body.quantity,
                    "modality": body.modality,
                }
            ]
        )

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
