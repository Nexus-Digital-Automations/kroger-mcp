"""Recipe health scoring.

Provides heuristic-based health scoring using the ingredient safety system and
real USDA ingredient data. Keyword vocabularies live in ``recipe_keywords``;
DB-backed cost estimation lives in ``recipe_cost`` (which shares the
``_recipe_content_hash`` cache-key helper defined here).

The public entry point (calculate_health_score) is cached in Redis with a
content-addressed key: any edit to a recipe's ingredients or servings produces a
new key, so writes self-invalidate and no write path needs to drop keys. Drift
in unkeyed inputs (USDA ingredient-text backfill) expires via TTL; user edits to
the safety ingredient list are keyed via the ``ingredients:version`` Redis
counter.
"""

import hashlib
import json
from typing import Any

from kroger_mcp.analytics.recipe_keywords import (
    _PENALTY_CAPS,
    _PENALTY_PER_MATCH,
    ALL_HEALTHY_KEYWORDS,
    CONVENIENCE_INDICATOR_SET,
    HEALTHY_CATEGORY_KEYWORDS,
    HEAVY_NEGATIVE_SET,
    NEUTRAL_STAPLE_SET,
    PROCESSED_INDICATOR_SET,
    SUGAR_KEYWORD_SET,
    WHOLE_FOOD_SIGNAL_SET,
)
from kroger_mcp.cache import cache_read_through, get_version

# Drift TTL for inputs the cache key cannot see (USDA ingredients_text backfill).
_HEALTH_TTL_SECONDS = 6 * 3600


def _grade(score: int) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    if score >= 35:
        return "D"
    return "F"


def _recipe_content_hash(recipe: dict[str, Any]) -> str:
    """16-hex content hash over the score/cost-relevant recipe fields.

    Includes quantity: recipe_cost.py's cost estimate scales with ingredient
    quantity, so a quantity-only edit must still bust its cached total (the
    health score doesn't use quantity, but sharing one hash just means an
    occasional harmless extra rebuild there).
    """
    payload = [
        [ing.get("name"), ing.get("product_id"), ing.get("quantity")]
        for ing in (recipe.get("ingredients") or [])
    ] + [recipe.get("servings")]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def calculate_health_score(
    recipe: dict[str, Any],
    names_only: bool = False,
    *,
    user_id: str,
) -> dict[str, Any]:
    """Redis-cached wrapper around the health-score computation.

    Key embeds the recipe content hash, the ``ingredients:version`` counter
    (bumped by analytics.ingredients when any user edits the safety pattern
    list), and user_id (the score depends on the viewer's own custom
    ingredients/overrides), so recipe edits, safety-list edits, and viewing
    as a different user all miss cleanly.
    """
    rid = recipe.get("id") or "adhoc"
    ing_ver = get_version("ingredients:version") or 0
    mode = "n" if names_only else "f"
    key = f"recipe:health:{rid}:{_recipe_content_hash(recipe)}:ing{ing_ver}:{mode}:u{user_id}"
    return cache_read_through(
        key,
        _HEALTH_TTL_SECONDS,
        lambda: _calculate_health_score_uncached(recipe, names_only, user_id=user_id),
    )


def _calculate_health_score_uncached(
    recipe: dict[str, Any],
    names_only: bool = False,
    *,
    user_id: str,
) -> dict[str, Any]:
    """
    Calculate a health score (0-100) for a recipe using real ingredient data.

    Looks up actual product ingredient lists from the USDA FoodData Central
    database (cached locally) and scans them against the BAD_INGREDIENTS list.
    This catches real additives (sodium nitrite, HFCS, artificial colors, etc.)
    that would never appear in a human-readable ingredient name.

    Args:
        recipe: Recipe dict with "ingredients" list.
        names_only: If True, use only cached DB data (no live USDA API calls).
                    Used for list views where speed matters.

    Returns:
        Dict with score, grade, confidence, flags, categories_detected, etc.
    """
    from .ingredients import check_product_safety

    ingredients = recipe.get("ingredients") or []
    total = len(ingredients)

    if total == 0:
        return {
            "score": 0,
            "grade": "N/A",
            "confidence": "none",
            "flags": [],
            "categories_detected": [],
            "bonus_applied": 0,
            "linked_ingredients": 0,
            "total_ingredients": 0,
        }

    # Batch-load linked products from DB (includes cached ingredients_text)
    product_info: dict[str, dict[str, str]] = {}
    linked_ids = [ing["product_id"] for ing in ingredients if ing.get("product_id")]
    if linked_ids:
        try:
            from .database import get_db_connection

            conn = get_db_connection()
            try:
                placeholders = ",".join("?" * len(linked_ids))
                rows = conn.execute(
                    "SELECT product_id, upc, description, brand, "
                    "ingredients_text "
                    "FROM products "
                    f"WHERE product_id IN ({placeholders})",
                    linked_ids,
                ).fetchall()
                for row in rows:
                    product_info[row["product_id"]] = {
                        "description": row["description"] or "",
                        "brand": row["brand"] or "",
                        "upc": row["upc"] or "",
                        "ingredients_text": row["ingredients_text"] or "",
                    }
            finally:
                conn.close()
        except Exception:
            pass

    # If not names_only, fetch missing USDA data for products without
    # cached ingredients_text
    if not names_only:
        _fetch_missing_usda_data(product_info)

    # Accumulate penalties and flags
    linked_count = 0
    usda_count = 0
    severity_counts: dict[str, int] = {"critical": 0, "warning": 0, "watch": 0}
    flags: list[dict[str, Any]] = []

    for ing in ingredients:
        pid = ing.get("product_id")
        ing_name = ing.get("name", "")

        if pid and pid in product_info:
            linked_count += 1
            info = product_info[pid]

            # Prefer real ingredient list from USDA
            if info["ingredients_text"]:
                scan_text = info["ingredients_text"]
                usda_count += 1
            else:
                # Fall back to product description + brand
                scan_text = info["description"]
                if info["brand"]:
                    scan_text = info["brand"] + " " + scan_text
        elif pid:
            linked_count += 1
            scan_text = ing_name
        else:
            scan_text = ing_name

        result = check_product_safety(scan_text, user_id=user_id)
        for match in result.matches:
            sev = match.severity.value
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            flags.append(
                {
                    "ingredient": ing_name,
                    "flag": match.ingredient_name,
                    "severity": sev,
                    "reason": match.reason,
                }
            )

    # Compute BAD_INGREDIENTS penalty (capped per severity)
    bad_ing_penalty = 0
    for sev, count in severity_counts.items():
        raw = count * _PENALTY_PER_MATCH[sev]
        capped = min(raw, _PENALTY_CAPS[sev])
        bad_ing_penalty += capped

    # --- Build-up scoring model ---

    # Collect all ingredient names (lowercase) for scanning
    ing_names_lower = [(ing.get("name") or "").lower() for ing in ingredients]
    all_ing_text = " ".join(ing_names_lower)

    # 1. Category coverage: 7 pts per healthy category (max 35)
    categories_detected: list[str] = []
    for cat, keywords in HEALTHY_CATEGORY_KEYWORDS.items():
        if any(kw in all_ing_text for kw in keywords):
            categories_detected.append(cat)
    cat_score = min(len(categories_detected) * 7, 35)

    # 2. Ingredient quality ratio: of NON-STAPLE ingredients, how many are
    # healthy? Excluding staples (salt, broth, soy sauce, etc.) stops common
    # pantry items from dragging the ratio down on otherwise-clean recipes.
    non_staple = [
        name for name in ing_names_lower if not any(staple in name for staple in NEUTRAL_STAPLE_SET)
    ]
    denom = max(1, len(non_staple))
    quality_hits = sum(1 for name in non_staple if any(kw in name for kw in ALL_HEALTHY_KEYWORDS))
    quality_score = round((quality_hits / denom) * 30)

    # 3. Whole food signals: small bonus when authors explicitly say
    # "fresh"/"organic"/etc. Capped at 5 so recipes that just write "broccoli"
    # aren't punished for omitting marketing adjectives.
    whole_hits = sum(
        1 for name in ing_names_lower if any(sig in name for sig in WHOLE_FOOD_SIGNAL_SET)
    )
    whole_score = min(round((whole_hits / total) * 15), 5) if total else 0

    # 4. Processed indicators penalty (max -15, -5 each)
    proc_penalty = 0
    for name in ing_names_lower:
        # Exclude "instant pot" from matching "instant"
        scan = name.replace("instant pot", "")
        if any(ind in scan for ind in PROCESSED_INDICATOR_SET):
            proc_penalty += 5
    proc_penalty = min(proc_penalty, 15)

    # 5. Convenience indicators penalty (max -8, -3 each)
    conv_penalty = 0
    for name in ing_names_lower:
        if any(ind in name for ind in CONVENIENCE_INDICATOR_SET):
            conv_penalty += 3
    conv_penalty = min(conv_penalty, 8)

    # 6. Heavy negatives penalty (max -10, -3 each)
    heavy_penalty = 0
    for name in ing_names_lower:
        if any(ind in name for ind in HEAVY_NEGATIVE_SET):
            heavy_penalty += 3
    heavy_penalty = min(heavy_penalty, 10)

    # 7. Sugar penalty (max -6, -2 each)
    sugar_penalty = 0
    for name in ing_names_lower:
        if "stevia" in name:
            continue
        # Check longer patterns first to avoid double-counting
        if any(kw in name for kw in SUGAR_KEYWORD_SET):
            sugar_penalty += 2
    sugar_penalty = min(sugar_penalty, 6)

    base = 20
    bonus = cat_score + quality_score + whole_score
    total_penalty = proc_penalty + conv_penalty + heavy_penalty + sugar_penalty + bad_ing_penalty
    score = max(0, min(100, base + bonus - total_penalty))

    # A CRITICAL-severity ingredient (e.g. trans fats, artificial dyes) must
    # never be diluted into an "acceptable"/"good" headline grade by an
    # otherwise produce-heavy ingredient list -- cap below the C threshold.
    if severity_counts.get("critical", 0) > 0:
        score = min(score, 49)

    # Confidence based on how much real data we had
    if usda_count == total:
        confidence = "high"
    elif usda_count >= total / 2:
        confidence = "medium"
    elif linked_count >= total / 2:
        confidence = "low"
    else:
        confidence = "low"

    return {
        "score": score,
        "grade": _grade(score),
        "confidence": confidence,
        "flags": flags,
        "categories_detected": categories_detected,
        "bonus_applied": bonus,
        "linked_ingredients": linked_count,
        "usda_ingredients": usda_count,
        "total_ingredients": total,
    }


def _fetch_missing_usda_data(
    product_info: dict[str, dict[str, str]],
) -> None:
    """
    For products without cached ingredients_text, fetch from USDA
    (by UPC first, then by name) and update the local DB cache.

    Modifies product_info in place.
    """
    try:
        from .usda import fetch_ingredients_by_name, fetch_ingredients_by_upc
    except ImportError:
        return

    products_to_update: list[tuple] = []

    for pid, info in product_info.items():
        if info["ingredients_text"]:
            continue

        ingredients_text = None

        # Try UPC first
        upc = info.get("upc", "")
        if upc:
            ingredients_text = fetch_ingredients_by_upc(upc)

        # Fall back to name search
        if not ingredients_text and info.get("description"):
            ingredients_text = fetch_ingredients_by_name(info["description"], info.get("brand", ""))

        if ingredients_text:
            info["ingredients_text"] = ingredients_text
            products_to_update.append((ingredients_text, pid))

    # Batch-update DB cache
    if products_to_update:
        try:
            from .database import get_db_connection

            conn = get_db_connection()
            try:
                for ing_text, pid in products_to_update:
                    conn.execute(
                        "UPDATE products SET ingredients_text = ? " "WHERE product_id = ?",
                        (ing_text, pid),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
