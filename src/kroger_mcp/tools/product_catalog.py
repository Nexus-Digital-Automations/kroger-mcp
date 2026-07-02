"""Local product-catalog read-through for Kroger product *detail*.

Flips the local ``products`` + ``price_history`` tables (already written on
every search/purchase) from write-only into a read-through mirror, so most
product-detail reads are served locally and the shared Kroger app's ~10k/day
Products budget stretches to many more users.

Contract: returns the raw Kroger-shaped record (the dict under the API's
``data`` key) so both surfaces — web ``_extract_product`` and the MCP
``format_details`` — run their existing extractor unchanged. Search is NOT
served from here: the local mirror only holds previously-seen products, so a
local search would return an incomplete subset (a correctness bug); search
stays on the Redis cache.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from kroger_api.kroger_api import KrogerAPI

from kroger_mcp.cache import cache_read_through
from kroger_mcp.tools.shared import kroger_cache_key

logger = logging.getLogger(__name__)

# Local read-through tunables.
#   - Metadata (name/brand/ingredients) serves from local freely; it rarely
#     changes.
#   - Price serves locally only while the latest price_history observation is
#     within this window; past it we refresh that one product from Kroger.
# Lower the window for price-sensitive surfaces via the env var.
_DETAIL_CACHE_TTL = 3600  # Redis TTL for the Kroger detail fallback
_PRICE_FRESHNESS_SECONDS = int(os.environ.get("KROGER_PRICE_FRESHNESS_SECONDS", "43200"))  # 12h


def _parse_observed_at(value: object) -> datetime | None:
    """Parse a price_history.observed_at string to a naive datetime, or None.

    Tolerates both SQLite's 'T'-separated ISO and Postgres' space-separated
    (and possibly tz-aware) form. Unparseable → None so the caller treats the
    price as stale and refreshes rather than throwing.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T"))
    except (ValueError, AttributeError):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _local_price_block(regular: object, sale: object) -> dict:
    """Reconstruct Kroger's items[].price shape from local price columns."""
    block: dict[str, object] = {}
    if regular is not None:
        block["regular"] = regular
    if sale is not None:
        block["promo"] = sale
    return block


def _detail_price_from_record(record: dict) -> tuple[float | None, float | None]:
    """Pull (regular, promo) out of a raw Kroger detail record's first item."""
    items = record.get("items") or [{}]
    first = items[0] if items else {}
    if not isinstance(first, dict):
        first = vars(first) if hasattr(first, "__dict__") else {}
    price = first.get("price", {})
    if not isinstance(price, dict):
        price = vars(price) if hasattr(price, "__dict__") else {}
    return price.get("regular"), price.get("promo")


def _aisle_descriptions(record: dict) -> list[str]:
    """Pull aisle description strings out of a raw Kroger detail record."""
    out: list[str] = []
    for aisle in record.get("aisleLocations") or []:
        desc = aisle.get("description") if isinstance(aisle, dict) else None
        if desc:
            out.append(desc)
    return out


def _upsert_product_metadata(record: dict) -> None:
    """Persist name/brand/upc/category from a Kroger detail record (best-effort).

    Mirrors the COALESCE upsert in product_tools._cache_usda_ingredients so a
    sparse payload never nulls out previously-good metadata, and never touches
    ingredients_text (owned by the USDA cache path). When the product's aisle
    data reads as a spice aisle, caches category_type='spice' — but never
    overrides a user's manual categorization (category_override = 1).
    """
    from ..analytics.database import get_db_cursor
    from ..analytics.ingredients import category_type_from_aisles

    pid = record.get("productId") or record.get("upc")
    if not pid:
        return
    now = datetime.now().isoformat()
    category = category_type_from_aisles(_aisle_descriptions(record))
    try:
        with get_db_cursor() as cursor:
            if category is not None:
                cursor.execute(
                    "INSERT INTO products (product_id, upc, description, brand, "
                    "category_type, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(product_id) DO UPDATE SET "
                    "upc = COALESCE(excluded.upc, products.upc), "
                    "description = COALESCE(excluded.description, products.description), "
                    "brand = COALESCE(excluded.brand, products.brand), "
                    "category_type = CASE WHEN COALESCE(products.category_override, 0) = 0 "
                    "THEN excluded.category_type ELSE products.category_type END, "
                    "updated_at = excluded.updated_at",
                    (pid, record.get("upc"), record.get("description"),
                     record.get("brand"), category, now, now),
                )
            else:
                cursor.execute(
                    "INSERT INTO products (product_id, upc, description, brand, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(product_id) DO UPDATE SET "
                    "upc = COALESCE(excluded.upc, products.upc), "
                    "description = COALESCE(excluded.description, products.description), "
                    "brand = COALESCE(excluded.brand, products.brand), "
                    "updated_at = excluded.updated_at",
                    (pid, record.get("upc"), record.get("description"),
                     record.get("brand"), now, now),
                )
    except Exception:
        logger.debug("product metadata upsert skipped (best-effort)", exc_info=True)


def product_detail_read_through(
    client: KrogerAPI,
    product_id: str,
    location_id: str,
    *,
    ttl_seconds: int = _DETAIL_CACHE_TTL,
    freshness_seconds: int | None = None,
) -> dict | None:
    """Resolve one product's detail record, serving from the local catalog when
    possible to spare the shared Kroger budget.

    Returns the raw Kroger-shaped record (the dict under the API's ``data`` key),
    or ``None`` when the product is unknown everywhere.

    Local hit (metadata present AND latest price for this location fresh) →
    reconstruct the record from `products` + `price_history` with **zero Kroger
    calls**; image/nutrition/aisle fields are absent (not mirrored locally),
    name/brand/price/ingredients are present. Otherwise fetch from Kroger
    (through the Redis cache), then refresh the local tables.
    """
    if freshness_seconds is None:
        freshness_seconds = _PRICE_FRESHNESS_SECONDS

    from ..analytics.database import ensure_initialized, get_db_cursor
    from ..analytics.deals import record_price_observation

    ensure_initialized()

    meta = None
    price_row = None
    try:
        with get_db_cursor() as cursor:
            meta = cursor.execute(
                "SELECT description, brand, upc, ingredients_text "
                "FROM products WHERE product_id = ?",
                (product_id,),
            ).fetchone()
            price_row = cursor.execute(
                "SELECT regular_price, sale_price, observed_at FROM price_history "
                "WHERE product_id = ? AND location_id = ? "
                "ORDER BY observed_at DESC LIMIT 1",
                (product_id, location_id),
            ).fetchone()
    except Exception:
        meta = price_row = None

    is_fresh = False
    if price_row is not None:
        observed = _parse_observed_at(price_row["observed_at"])
        if observed is not None:
            age = (datetime.now() - observed).total_seconds()
            is_fresh = 0 <= age <= freshness_seconds

    # Local hit — serve without touching Kroger.
    if meta is not None and meta["description"] and is_fresh and price_row is not None:
        record: dict = {
            "productId": product_id,
            "description": meta["description"],
            "brand": meta["brand"] or "",
            "items": [{"price": _local_price_block(
                price_row["regular_price"], price_row["sale_price"])}],
        }
        if meta["upc"]:
            record["upc"] = meta["upc"]
        if meta["ingredients_text"]:
            record["ingredients"] = meta["ingredients_text"]
        return record

    # Miss / stale / missing metadata — fetch from Kroger (Redis-cached) + refresh.
    cache_key = kroger_cache_key(client, "product_detail", pid=product_id, location=location_id)
    raw = cache_read_through(
        cache_key,
        ttl_seconds,
        lambda: client.product.get_product(product_id=product_id, location_id=location_id),
    )
    record_obj = raw.get("data") if isinstance(raw, dict) else getattr(raw, "data", None)
    if not record_obj:
        return None
    rec = record_obj if isinstance(record_obj, dict) else dict(vars(record_obj))

    _upsert_product_metadata(rec)
    regular, promo = _detail_price_from_record(rec)
    on_sale = promo is not None and regular is not None and promo < regular
    try:
        record_price_observation(
            product_id, regular, promo if on_sale else None, location_id, source="detail"
        )
    except Exception:
        logger.debug("detail price observation skipped (best-effort)", exc_info=True)
    return record_obj
