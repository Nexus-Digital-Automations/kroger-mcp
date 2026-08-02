"""Product safety checking: single-product status, batch, and async wrappers."""

import asyncio
import logging
from typing import Any

from ..database import ensure_initialized, get_db_cursor
from ..ingredients import check_product_safety, resolve_scan_text
from ._cache import _cached_product_safety
from ._common import _resolve_user_id
from .blocked_list import get_all_blocked_product_ids, is_product_blocked
from .ingredient_prefs import get_disabled_ingredients
from .models import ProductSafetyStatus, SafetyStatus
from .safe_list import get_all_safe_product_ids, is_product_safe_listed

logger = logging.getLogger(__name__)


def _load_ingredients_text(product_ids: list[str]) -> dict[str, str]:
    """Batch-load cached label text for the given products.

    One query per request rather than one per product. Recipe scoring already
    does this lookup; without it the safety check only ever sees a product's
    name and grades ultra-processed items as clean (see `resolve_scan_text`).

    Returns:
        product_id -> non-empty ingredients_text. Products with no cached label
        are simply absent, as is every product when the lookup fails: a
        degraded name-only scan is bad, but failing a cart-add safety check
        outright is worse.
    """
    ids = [pid for pid in product_ids if pid]
    if not ids:
        return {}

    placeholders = ",".join("?" * len(ids))
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                f"SELECT product_id, ingredients_text FROM products "  # noqa: S608 - placeholders only
                f"WHERE product_id IN ({placeholders})",
                ids,
            )
            return {
                row["product_id"]: row["ingredients_text"]
                for row in cursor.fetchall()
                if row["ingredients_text"]
            }
    except Exception as exc:
        logger.warning(
            "safety label lookup failed for %d product(s) (%s); "
            "falling back to description-only scan",
            len(ids),
            exc,
        )
        return {}


def _status_from_score(score: float) -> SafetyStatus:
    """Map a safety score to its status band."""
    if score >= 90:
        return SafetyStatus.EXCELLENT
    if score >= 75:
        return SafetyStatus.GOOD
    if score >= 60:
        return SafetyStatus.ACCEPTABLE
    if score >= 45:
        return SafetyStatus.POOR
    return SafetyStatus.AVOID


def get_product_safety_status(
    product_id: str,
    description: str,
    brand: str | None = None,
    categories: list[str] | None = None,
    *, user_id: str,
) -> ProductSafetyStatus:
    """
    Get the complete safety status for a product, scoped to a user.

    This checks:
    1. If product is on the user's safe list (bypasses all checks)
    2. If product is on the user's blocked list
    3. If product matches any bad ingredients (respecting user's disabled set)

    Args:
        product_id: Kroger product ID
        description: Product description to scan
        brand: Product brand (not scanned)
        categories: Product categories
        user_id: User scope; None resolves via `_resolve_user_id`.

    Returns:
        ProductSafetyStatus with complete safety information
    """
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    if is_product_safe_listed(product_id, user_id=resolved):
        return ProductSafetyStatus(
            product_id=product_id,
            status=SafetyStatus.SAFE,
            is_safe_listed=True,
            is_blocked=False,
            blocked_reason=None,
            safety_result=None,
        )

    is_blocked, blocked_reason = is_product_blocked(product_id, user_id=resolved)
    if is_blocked:
        return ProductSafetyStatus(
            product_id=product_id,
            status=SafetyStatus.BLOCKED,
            is_safe_listed=False,
            is_blocked=True,
            blocked_reason=blocked_reason,
            safety_result=None,
        )

    disabled = get_disabled_ingredients(user_id=resolved)

    # Scan the real label when we have one -- the description is the product's
    # name, which never lists its additives.
    scan_text = resolve_scan_text(
        description,
        brand,
        _load_ingredients_text([product_id]).get(product_id),
    )

    safety_result = check_product_safety(
        description=scan_text,
        brand=brand,
        categories=categories,
        disabled_ingredients=disabled,
        user_id=resolved,
    )

    return ProductSafetyStatus(
        product_id=product_id,
        status=_status_from_score(safety_result.score),
        is_safe_listed=False,
        is_blocked=False,
        blocked_reason=None,
        safety_result=safety_result,
    )


def check_products_safety_batch(
    products: list[dict[str, Any]],
    user_id: str,
) -> list[ProductSafetyStatus]:
    """
    Check safety status for multiple products efficiently, scoped to a user.

    Args:
        products: List of product dicts with 'product_id', 'description', 'brand'
        user_id: User scope; None resolves via `_resolve_user_id`.

    Returns:
        List of ProductSafetyStatus objects
    """
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    # Pre-load safe and blocked lists for O(1) lookups per user.
    safe_ids = get_all_safe_product_ids(user_id=resolved)
    blocked_ids = get_all_blocked_product_ids(user_id=resolved)
    disabled = get_disabled_ingredients(user_id=resolved)

    blocked_reasons: dict[str, str] = {}
    if blocked_ids:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT product_id, blocked_reason FROM blocked_products WHERE user_id = ?",
                (resolved,),
            )
            for row in cursor.fetchall():
                blocked_reasons[row["product_id"]] = row["blocked_reason"]

    # One label lookup for the whole batch, not one per product.
    labels = _load_ingredients_text([p.get("product_id", "") for p in products])

    results = []
    for product in products:
        product_id = product.get("product_id", "")
        description = product.get("description", "")
        brand = product.get("brand")

        # Check safe list
        if product_id in safe_ids:
            results.append(
                ProductSafetyStatus(
                    product_id=product_id,
                    status=SafetyStatus.SAFE,
                    is_safe_listed=True,
                    is_blocked=False,
                    blocked_reason=None,
                    safety_result=None,
                )
            )
            continue

        # Check blocked list
        if product_id in blocked_ids:
            results.append(
                ProductSafetyStatus(
                    product_id=product_id,
                    status=SafetyStatus.BLOCKED,
                    is_safe_listed=False,
                    is_blocked=True,
                    blocked_reason=blocked_reasons.get(product_id),
                    safety_result=None,
                )
            )
            continue

        # Check ingredients (memoized in Redis when a product_id is present).
        # The memo key hashes the scanned text, so switching from name to label
        # misses stale name-scanned entries rather than returning them.
        safety_result = _cached_product_safety(
            user_id=resolved,
            product_id=product_id,
            description=resolve_scan_text(description, brand, labels.get(product_id)),
            brand=brand,
            disabled=disabled,
        )

        results.append(
            ProductSafetyStatus(
                product_id=product_id,
                status=_status_from_score(safety_result.score),
                is_safe_listed=False,
                is_blocked=False,
                blocked_reason=None,
                safety_result=safety_result,
            )
        )

    return results


# ==================== ASYNC WRAPPERS ====================
# Use these from async tool handlers to avoid blocking the event loop.


async def check_products_safety_batch_async(
    products: list[dict[str, Any]],
    user_id: str,
) -> list[ProductSafetyStatus]:
    """Async wrapper for check_products_safety_batch() — runs in thread pool."""
    return await asyncio.to_thread(check_products_safety_batch, products, user_id=user_id)


async def get_product_safety_status_async(
    product_id: str,
    description: str,
    brand: str | None = None,
    categories: list[str] | None = None,
    *, user_id: str,
) -> ProductSafetyStatus:
    """Async wrapper for get_product_safety_status() — runs in thread pool."""
    return await asyncio.to_thread(
        get_product_safety_status,
        product_id,
        description,
        brand,
        categories,
        user_id=user_id,
    )
