"""
Recipe health scoring and cost estimation.

Provides heuristic-based health scoring using the ingredient safety system
and DB-backed cost estimation using price_history data.

Both public entry points (calculate_health_score, estimate_recipe_cost) are
cached in Redis with content-addressed keys: any edit to a recipe's
ingredients or servings produces a new key, so writes self-invalidate and no
write path needs to drop keys. Drift in unkeyed inputs (USDA ingredient-text
backfill, price_history rows) expires via TTL; user edits to the safety
ingredient list are keyed via the ``ingredients:version`` Redis counter.
"""

import hashlib
import json
from typing import Any

from kroger_mcp.cache import cache_read_through, get_version

# ---------------------------------------------------------------------------
# Healthy category keyword matching
# ---------------------------------------------------------------------------

HEALTHY_CATEGORIES: dict[str, list[str]] = {
    "produce": [
        "vegetable",
        "spinach",
        "kale",
        "broccoli",
        "carrot",
        "tomato",
        "onion",
        "garlic",
        "pepper",
        "lettuce",
        "cucumber",
        "zucchini",
        "asparagus",
        "apple",
        "banana",
        "berry",
        "lemon",
        "lime",
        "celery",
        "mushroom",
        "peas",
        "squash",
        "eggplant",
        "cabbage",
        "bok choy",
        "sweet potato",
        "potato",
        "orange",
        "cranberr",
        "mango",
        "pineapple",
        "grapefruit",
        "scallion",
        "radish",
        "beet",
        "artichoke",
        "corn",
        "romaine",
        "arugula",
        "chard",
        "watermelon",
        "pear",
        "peach",
        "plum",
        "jalapeno",
        "jalapeño",
        "leek",
        "shallot",
        "okra",
        "kohlrabi",
        "endive",
        "fennel bulb",
    ],
    "lean_protein": [
        "chicken",
        "turkey",
        "salmon",
        "tuna",
        "egg",
        "lentil",
        "chickpea",
        "black bean",
        "kidney bean",
        "tofu",
        "tempeh",
        "shrimp",
        "cod",
        "tilapia",
        "fish",
        "clam",
        "bean",
        "halibut",
        "sardine",
        "trout",
        "mackerel",
        "anchovy",
        "mussel",
        "oyster",
        "crab",
        "lobster",
        "scallop",
        "pork tenderloin",
        "sirloin",
        "flank steak",
        "edamame",
        "hummus",
    ],
    "whole_grain": [
        "brown rice",
        "quinoa",
        "oats",
        "whole wheat",
        "whole grain",
        "farro",
        "barley",
        "bulgur",
        "wild rice",
        "buckwheat",
        "millet",
        "steel-cut oats",
        "rolled oats",
        "whole-wheat pasta",
        "whole wheat pasta",
    ],
    "healthy_fat": [
        "olive oil",
        "avocado",
        "almond",
        "walnut",
        "cashew",
        "flaxseed",
        "chia",
        "hemp seed",
        "pecan",
        "pine nut",
        "pistachio",
        "sesame seed",
        "pumpkin seed",
        "sunflower seed",
        "tahini",
        "avocado oil",
        "coconut oil",
    ],
    "herbs_spices": [
        "basil",
        "oregano",
        "thyme",
        "rosemary",
        "cilantro",
        "parsley",
        "mint",
        "dill",
        "cumin",
        "turmeric",
        "ginger",
        "cinnamon",
        "paprika",
        "cayenne",
        "sage",
        "bay leaf",
        "coriander",
        "cardamom",
        "saffron",
        "nutmeg",
        "clove",
        "chive",
        "fennel",
        "tarragon",
        "five-spice",
        "chili powder",
        "smoked paprika",
        "garlic powder",
        "onion powder",
        "red pepper flake",
        "italian seasoning",
        "za'atar",
        "old bay",
    ],
}

# Penalty caps per severity
_PENALTY_CAPS = {
    "critical": 45,
    "warning": 24,
    "watch": 12,
}

# Per-match penalty per severity
_PENALTY_PER_MATCH = {
    "critical": 15,
    "warning": 8,
    "watch": 3,
}

# ---------------------------------------------------------------------------
# Build-up scoring: ingredient-name signal keywords
# ---------------------------------------------------------------------------

WHOLE_FOOD_SIGNALS: list[str] = [
    "fresh",
    "whole",
    "organic",
    "raw",
    "grass-fed",
    "grass fed",
    "wild-caught",
    "wild caught",
    "bone-in",
    "skin-on",
]

PROCESSED_INDICATORS: list[str] = [
    "cream of",
    "condensed",
    "pre-made",
    "pre-packaged",
    "store-bought",
    "cooking spray",
    "liquid smoke",
    "instant",
]

CONVENIENCE_INDICATORS: list[str] = [
    "rotisserie",
    "canned",
    "breadcrumbs",
    "panko",
    "marinara sauce",
    "curry paste",
    "better than bouillon",
]

HEAVY_NEGATIVES: list[str] = [
    "bacon",
    "sausage",
    "andouille",
]

SUGAR_KEYWORDS: list[str] = [
    "brown sugar",
    "powdered sugar",
    "corn syrup",
    "sugar",
]

# Neutral cooking staples — present in almost every recipe but not "unhealthy".
# Excluded from the quality-ratio denominator so they don't drag the score
# down when paired with otherwise-healthy ingredients.
NEUTRAL_STAPLES: list[str] = [
    "salt",
    "pepper",
    "black pepper",
    "white pepper",
    "water",
    "ice",
    "vinegar",
    "rice vinegar",
    "balsamic",
    "apple cider vinegar",
    "soy sauce",
    "tamari",
    "fish sauce",
    "worcestershire",
    "broth",
    "stock",
    "bouillon",
    "lemon juice",
    "lime juice",
    "orange juice",
    "mustard",
    "dijon",
    "ketchup",
    "hot sauce",
    "sriracha",
    "tabasco",
    "honey",
    "maple syrup",
    "yogurt",
    "greek yogurt",
    "milk",
    "butter",
    "cream",
    "cheese",
    "flour",
    "cornstarch",
    "baking soda",
    "baking powder",
    "yeast",
    "egg white",
    "egg yolk",
    "vanilla",
    "vanilla extract",
]


# ---------------------------------------------------------------------------
# Precomputed matchers (built ONCE at import, not per scoring call).
#
# The scoring loop previously rebuilt `all_keywords` (~180 entries flattened
# from HEALTHY_CATEGORIES) on every call and re-iterated each keyword list as a
# Python list for every ingredient name. We hoist the immutable collections to
# module level so the allocation happens once.
#
# Matching semantics are preserved EXACTLY: every check below remains a
# substring test (`kw in name`), because the keyword vocabularies rely on it —
# e.g. "bean" must match "beans"/"green beans", "berry" matches "blueberry",
# "cranberr" matches "cranberries", and multi-word entries like "sweet potato"
# / "cream of" / "grass-fed" are inherently substrings. Switching to whole-word
# set intersection would change scores, so we do NOT do that here; the win is
# purely from not rebuilding these collections per call.
# ---------------------------------------------------------------------------

# Flattened, de-duplicated healthy keywords for the quality-ratio scan.
ALL_HEALTHY_KEYWORDS: frozenset[str] = frozenset(
    kw for kws in HEALTHY_CATEGORIES.values() for kw in kws
)

# Per-category keyword sets for category-coverage detection (preserves order of
# detection via HEALTHY_CATEGORIES iteration; membership uses these sets).
HEALTHY_CATEGORY_KEYWORDS: dict[str, frozenset[str]] = {
    cat: frozenset(kws) for cat, kws in HEALTHY_CATEGORIES.items()
}

WHOLE_FOOD_SIGNAL_SET: frozenset[str] = frozenset(WHOLE_FOOD_SIGNALS)
PROCESSED_INDICATOR_SET: frozenset[str] = frozenset(PROCESSED_INDICATORS)
CONVENIENCE_INDICATOR_SET: frozenset[str] = frozenset(CONVENIENCE_INDICATORS)
HEAVY_NEGATIVE_SET: frozenset[str] = frozenset(HEAVY_NEGATIVES)
SUGAR_KEYWORD_SET: frozenset[str] = frozenset(SUGAR_KEYWORDS)
NEUTRAL_STAPLE_SET: frozenset[str] = frozenset(NEUTRAL_STAPLES)


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


# Drift TTLs for inputs the cache key cannot see (see module docstring).
_HEALTH_TTL_SECONDS = 6 * 3600  # USDA ingredients_text backfill
_COST_TTL_SECONDS = 3600  # price_history / products price rows


def _recipe_content_hash(recipe: dict[str, Any]) -> str:
    """16-hex content hash over the score-relevant recipe fields."""
    payload = [
        [ing.get("name"), ing.get("product_id")]
        for ing in (recipe.get("ingredients") or [])
    ] + [recipe.get("servings")]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def calculate_health_score(
    recipe: dict[str, Any],
    names_only: bool = False,
) -> dict[str, Any]:
    """Redis-cached wrapper around the health-score computation.

    Key embeds the recipe content hash plus the ``ingredients:version``
    counter (bumped by analytics.ingredients when the user edits the safety
    pattern list), so both recipe edits and safety-list edits miss cleanly.
    """
    rid = recipe.get("id") or "adhoc"
    ing_ver = get_version("ingredients:version") or 0
    mode = "n" if names_only else "f"
    key = f"recipe:health:{rid}:{_recipe_content_hash(recipe)}:ing{ing_ver}:{mode}"
    return cache_read_through(
        key,
        _HEALTH_TTL_SECONDS,
        lambda: _calculate_health_score_uncached(recipe, names_only),
    )


def _calculate_health_score_uncached(
    recipe: dict[str, Any],
    names_only: bool = False,
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

        result = check_product_safety(scan_text)
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
        name
        for name in ing_names_lower
        if not any(staple in name for staple in NEUTRAL_STAPLE_SET)
    ]
    denom = max(1, len(non_staple))
    quality_hits = sum(
        1 for name in non_staple if any(kw in name for kw in ALL_HEALTHY_KEYWORDS)
    )
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


# ---------------------------------------------------------------------------
# Cost estimation (DB-only)
# ---------------------------------------------------------------------------


def estimate_recipe_cost(
    recipe: dict[str, Any],
    location_id: str | None = None,
) -> dict[str, Any]:
    """Redis-cached wrapper around the DB-only cost estimation.

    Content-addressed key (recipe edits self-invalidate); price drift expires
    via the 1h TTL. estimate_recipe_cost_with_api builds on this cached call.
    """
    rid = recipe.get("id") or "adhoc"
    key = f"recipe:cost:{rid}:{_recipe_content_hash(recipe)}:{location_id or 'any'}"
    return cache_read_through(
        key,
        _COST_TTL_SECONDS,
        lambda: _estimate_recipe_cost_uncached(recipe, location_id),
    )


def _estimate_recipe_cost_uncached(
    recipe: dict[str, Any],
    location_id: str | None = None,
) -> dict[str, Any]:
    """
    Estimate recipe cost using local price_history / products DB tables only.

    Returns a dict with total_cost, cost_per_serving, breakdown, etc.
    """
    try:
        from .database import get_db_connection

        conn = get_db_connection()
    except Exception:
        return _empty_cost(recipe, "Database unavailable")

    try:
        return _estimate_cost_with_conn(recipe, location_id, conn)
    finally:
        conn.close()


def _empty_cost(recipe: dict[str, Any], note: str) -> dict[str, Any]:
    servings = max(1, recipe.get("servings") or 1)
    return {
        "total_cost": None,
        "cost_per_serving": None,
        "currency": "USD",
        "servings": servings,
        "confidence": "low",
        "note": note,
        "breakdown": [],
    }


def _estimate_cost_with_conn(
    recipe: dict[str, Any],
    location_id: str | None,
    conn,
) -> dict[str, Any]:
    ingredients = recipe.get("ingredients") or []
    servings = max(1, recipe.get("servings") or 1)

    breakdown: list[dict[str, Any]] = []
    total_cost = 0.0
    priced_count = 0

    for ing in ingredients:
        ing_name = ing.get("name", "")
        pid = ing.get("product_id")

        entry: dict[str, Any] = {
            "ingredient": ing_name,
            "product_id": pid,
            "matched_description": None,
            "price": None,
            "on_sale": None,
            "regular_price": None,
            "sale_price": None,
            "price_source": "unknown",
            "last_observed": None,
        }

        if pid:
            row = _fetch_price_by_product_id(conn, pid, location_id)
            if row:
                effective, entry = _apply_price_row(row, entry, "exact")
                total_cost += effective
                priced_count += 1
        else:
            row = _fetch_price_by_name(conn, ing_name, location_id)
            if row:
                effective, entry = _apply_price_row(row, entry, "estimated")
                total_cost += effective
                priced_count += 1

        breakdown.append(entry)

    # Build result
    total_count = len(ingredients)
    unknown_count = total_count - priced_count

    if priced_count == 0:
        final_total = None
        cost_per_serving = None
    else:
        final_total = round(total_cost, 2)
        cost_per_serving = round(total_cost / servings, 2)

    if priced_count == total_count:
        confidence = "high"
    elif priced_count >= total_count / 2:
        confidence = "medium"
    else:
        confidence = "low"

    notes = []
    if unknown_count > 0:
        notes.append(f"{unknown_count} ingredient(s) have no price data")
    note_str = "; ".join(notes) if notes else None

    return {
        "total_cost": final_total,
        "cost_per_serving": cost_per_serving,
        "currency": "USD",
        "servings": servings,
        "confidence": confidence,
        "note": note_str,
        "breakdown": breakdown,
    }


def _fetch_price_by_product_id(conn, product_id: str, location_id: str | None):
    """Fetch best price row from price_history for a known product_id."""
    try:
        row = conn.execute(
            """
            SELECT ph.regular_price, ph.sale_price, ph.on_sale,
                   ph.observed_at, ph.location_id,
                   p.description AS product_description, p.brand
            FROM price_history ph
            LEFT JOIN products p ON p.product_id = ph.product_id
            WHERE ph.product_id = ?
            ORDER BY
                CASE WHEN ph.location_id = ? THEN 0 ELSE 1 END,
                ph.observed_at DESC
            LIMIT 1
            """,
            (product_id, location_id or ""),
        ).fetchone()
        return row
    except Exception:
        return None


def _fetch_price_by_name(conn, name: str, location_id: str | None):
    """Text-search products table joined with price_history."""
    try:
        row = conn.execute(
            """
            SELECT p.product_id, p.description AS product_description, p.brand,
                   ph.regular_price, ph.sale_price, ph.on_sale,
                   ph.observed_at, ph.location_id
            FROM products p
            JOIN price_history ph ON ph.product_id = p.product_id
            WHERE LOWER(p.description) LIKE LOWER(?)
            ORDER BY
                CASE WHEN ph.location_id = ? THEN 0 ELSE 1 END,
                ph.observed_at DESC
            LIMIT 1
            """,
            (f"%{name}%", location_id or ""),
        ).fetchone()
        return row
    except Exception:
        return None


def _apply_price_row(
    row,
    entry: dict[str, Any],
    source: str,
) -> tuple:
    """Apply a DB row to an entry dict, return (effective_price, updated_entry)."""
    regular = row["regular_price"]
    sale = row["sale_price"]
    on_sale = bool(row["on_sale"]) if row["on_sale"] is not None else False
    effective = sale if (on_sale and sale is not None) else regular

    entry["matched_description"] = row.get("product_description")
    entry["price"] = effective
    entry["on_sale"] = on_sale
    entry["regular_price"] = regular
    entry["sale_price"] = sale
    entry["price_source"] = source
    entry["last_observed"] = row.get("observed_at")
    return effective, entry


# ---------------------------------------------------------------------------
# API-backed cost (for analyze action)
# ---------------------------------------------------------------------------


def estimate_recipe_cost_with_api(
    recipe: dict[str, Any],
    location_id: str | None,
    client,
) -> dict[str, Any]:
    """
    Same as estimate_recipe_cost but fills in unknown prices via Kroger API search.

    Args:
        recipe: Recipe dict.
        location_id: Preferred Kroger location ID.
        client: Authenticated KrogerAPI client.

    Returns:
        Same structure as estimate_recipe_cost, with api_search entries in breakdown.
    """
    # Start with DB estimate
    result = estimate_recipe_cost(recipe, location_id)
    api_fallback_note = None

    # Try to fill in unknowns via API
    for entry in result["breakdown"]:
        if entry["price_source"] != "unknown":
            continue
        ing_name = entry["ingredient"]
        try:
            search_results = client.products.search(
                term=ing_name,
                location_id=location_id,
                limit=1,
            )
            items = (search_results or {}).get("data") or []
            if items:
                item = items[0]
                prices = (item.get("items") or [{}])[0]
                price_info = prices.get("price") or {}
                regular = price_info.get("regular")
                promo = price_info.get("promo")
                on_sale = promo is not None and promo < regular if (promo and regular) else False
                effective = promo if on_sale else regular
                if effective is not None:
                    entry["price"] = effective
                    entry["on_sale"] = on_sale
                    entry["regular_price"] = regular
                    entry["sale_price"] = promo
                    entry["price_source"] = "api_search"
                    entry["matched_description"] = item.get("description")
        except Exception:
            api_fallback_note = "API search unavailable for some ingredients"

    # Recompute totals
    priced = [e for e in result["breakdown"] if e["price"] is not None]
    total_count = len(result["breakdown"])
    priced_count = len(priced)
    servings = result["servings"]

    if priced_count == 0:
        result["total_cost"] = None
        result["cost_per_serving"] = None
    else:
        total = sum(e["price"] for e in priced)
        result["total_cost"] = round(total, 2)
        result["cost_per_serving"] = round(total / servings, 2)

    if priced_count == total_count:
        result["confidence"] = "high"
    elif priced_count >= total_count / 2:
        result["confidence"] = "medium"
    else:
        result["confidence"] = "low"

    if api_fallback_note:
        result["api_fallback_note"] = api_fallback_note

    return result
