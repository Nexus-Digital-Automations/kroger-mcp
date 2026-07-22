"""Redis-backed memoization of product safety scans (best-effort)."""

import hashlib
import json
import logging

from kroger_mcp import cache

from ..ingredients import (
    AttributeMatch,
    IngredientMatch,
    SafetyResult,
    Severity,
    check_product_safety,
)

logger = logging.getLogger(__name__)

# Safety results are deterministic given (user, product text, ingredient
# ruleset, user's disabled set), so a hit short-circuits the O(patterns) scan.
# Keyed by product_id + a hash of (user_id, ingredients_version, sorted
# disabled set, description, brand) so any pattern/ingredient/text change or
# a different viewing user auto-invalidates (old keys simply expire).
_SAFETY_CACHE_TTL_SECONDS = 86_400  # 24h
_INGREDIENTS_VERSION_KEY = "ingredients:version"


def _safety_cache_key(
    user_id: str,
    product_id: str,
    description: str,
    brand: str | None,
    disabled: set[str],
) -> str:
    """Build the Redis key for a product's cached safety result.

    The hash component folds in the user (custom ingredients are per-user),
    the active ingredient ruleset version, the user's disabled-ingredient
    set, and the actual scanned text — every input (besides product_id
    itself) that changes the result. A description/brand change for the same
    product_id must miss the cache, not return a stale verdict for text that
    was never scanned. ``ingredients_version`` defaults to 0 when Redis is
    unavailable.
    """
    ingredients_version = cache.get_version(_INGREDIENTS_VERSION_KEY) or 0
    payload = f"{user_id}:{ingredients_version}:{sorted(disabled)}:{description}:{brand or ''}"
    # Non-security digest: this only namespaces a Redis cache key. usedforsecurity
    # =False documents intent and is the correct idiom (the payload is not secret).
    h = hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"safety:{product_id}:{h}"


def _serialize_safety_result(result: SafetyResult) -> str:
    """Serialize a SafetyResult to JSON, faithfully (round-trips exactly).

    ``SafetyResult.to_dict`` is lossy for reconstruction (it drops the
    ``ingredient_key``/``attribute_key`` fields and renames others for the API
    surface), so we serialize the full dataclass shape here instead.
    """
    return json.dumps(
        {
            "has_concerns": result.has_concerns,
            "highest_severity": (
                result.highest_severity.value
                if result.highest_severity is not None
                else None
            ),
            "score": result.score,
            "grade": result.grade,
            "matches": [
                {
                    "ingredient_key": m.ingredient_key,
                    "ingredient_name": m.ingredient_name,
                    "severity": m.severity.value,
                    "reason": m.reason,
                    "category": m.category,
                    "matched_text": m.matched_text,
                }
                for m in result.matches
            ],
            "positive_attributes": [
                {
                    "attribute_key": a.attribute_key,
                    "attribute_name": a.attribute_name,
                    "bonus": a.bonus,
                    "benefit": a.benefit,
                    "matched_text": a.matched_text,
                }
                for a in result.positive_attributes
            ],
        }
    )


def _deserialize_safety_result(raw: str) -> SafetyResult:
    """Reconstruct a SafetyResult from its serialized JSON form."""
    data = json.loads(raw)
    severity_raw = data.get("highest_severity")
    return SafetyResult(
        has_concerns=data["has_concerns"],
        highest_severity=Severity(severity_raw) if severity_raw is not None else None,
        matches=[
            IngredientMatch(
                ingredient_key=m["ingredient_key"],
                ingredient_name=m["ingredient_name"],
                severity=Severity(m["severity"]),
                reason=m["reason"],
                category=m["category"],
                matched_text=m["matched_text"],
            )
            for m in data.get("matches", [])
        ],
        positive_attributes=[
            AttributeMatch(
                attribute_key=a["attribute_key"],
                attribute_name=a["attribute_name"],
                bonus=a["bonus"],
                benefit=a["benefit"],
                matched_text=a["matched_text"],
            )
            for a in data.get("positive_attributes", [])
        ],
        score=data["score"],
        grade=data["grade"],
    )


def _cached_product_safety(
    user_id: str,
    product_id: str,
    description: str,
    brand: str | None,
    disabled: set[str],
) -> SafetyResult:
    """Return a product's SafetyResult, memoized in Redis (best-effort).

    On a cache hit the heavy ``check_product_safety`` scan is skipped. Only
    products with a non-empty ``product_id`` are cached. Every Redis access is
    wrapped so any failure (or an unavailable Redis) silently falls back to
    computing the result exactly as before — caching never fails a request.
    """
    redis_client = None
    key: str | None = None
    if product_id:
        try:
            redis_client = cache.get_redis()
            if redis_client is not None:
                key = _safety_cache_key(user_id, product_id, description, brand, disabled)
                hit = redis_client.get(key)
                # get_redis() sets decode_responses=True, so a hit is str; the
                # isinstance guard narrows the stub's bytes|str and degrades to
                # a recompute in the impossible bytes case.
                if isinstance(hit, str):
                    return _deserialize_safety_result(hit)
        except Exception as exc:
            logger.warning("safety cache read failed id=%s (%s)", product_id, exc)
            redis_client = None

    result = check_product_safety(
        description=description,
        brand=brand,
        disabled_ingredients=disabled,
        user_id=user_id,
    )

    if redis_client is not None and key is not None:
        try:
            redis_client.set(
                key,
                _serialize_safety_result(result),
                ex=_SAFETY_CACHE_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("safety cache write failed id=%s (%s)", product_id, exc)

    return result
