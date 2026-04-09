"""
Recipe health scoring and cost estimation.

Provides heuristic-based health scoring using the ingredient safety system
and DB-backed cost estimation using price_history data.
"""

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Healthy category keyword matching
# ---------------------------------------------------------------------------

HEALTHY_CATEGORIES: Dict[str, List[str]] = {
    "produce": [
        "vegetable", "spinach", "kale", "broccoli", "carrot", "tomato",
        "onion", "garlic", "pepper", "lettuce", "cucumber", "zucchini",
        "asparagus", "apple", "banana", "berry", "lemon", "lime",
        "celery", "mushroom", "peas", "squash", "eggplant", "cabbage",
        "bok choy", "sweet potato", "potato", "orange", "cranberr",
        "mango", "pineapple", "grapefruit", "scallion",
    ],
    "lean_protein": [
        "chicken", "turkey", "salmon", "tuna", "egg", "lentil",
        "chickpea", "black bean", "kidney bean", "tofu", "tempeh",
        "shrimp", "cod", "tilapia", "fish", "clam", "bean",
    ],
    "whole_grain": [
        "brown rice", "quinoa", "oats", "whole wheat", "whole grain",
        "farro", "barley", "bulgur",
    ],
    "healthy_fat": [
        "olive oil", "avocado", "almond", "walnut", "cashew",
        "flaxseed", "chia", "hemp seed", "pecan", "pine nut",
    ],
    "herbs_spices": [
        "basil", "oregano", "thyme", "rosemary", "cilantro", "parsley",
        "mint", "dill", "cumin", "turmeric", "ginger", "cinnamon",
        "paprika", "cayenne", "sage", "bay leaf", "coriander",
        "cardamom", "saffron", "nutmeg", "clove", "chive", "fennel",
        "tarragon", "five-spice",
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

WHOLE_FOOD_SIGNALS: List[str] = [
    "fresh", "whole", "organic", "raw", "grass-fed", "grass fed",
    "wild-caught", "wild caught", "bone-in", "skin-on",
]

PROCESSED_INDICATORS: List[str] = [
    "cream of", "condensed", "pre-made", "pre-packaged",
    "store-bought", "cooking spray", "liquid smoke", "instant",
]

CONVENIENCE_INDICATORS: List[str] = [
    "rotisserie", "canned", "breadcrumbs", "panko",
    "marinara sauce", "curry paste", "better than bouillon",
]

HEAVY_NEGATIVES: List[str] = [
    "bacon", "sausage", "andouille",
]

SUGAR_KEYWORDS: List[str] = [
    "brown sugar", "powdered sugar", "corn syrup", "sugar",
]


def _grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def calculate_health_score(
    recipe: Dict[str, Any],
    names_only: bool = False,
) -> Dict[str, Any]:
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
    product_info: Dict[str, Dict[str, str]] = {}
    linked_ids = [
        ing["product_id"]
        for ing in ingredients
        if ing.get("product_id")
    ]
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
    severity_counts: Dict[str, int] = {"critical": 0, "warning": 0, "watch": 0}
    flags: List[Dict[str, Any]] = []

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
            flags.append({
                "ingredient": ing_name,
                "flag": match.ingredient_name,
                "severity": sev,
                "reason": match.reason,
            })

    # Compute BAD_INGREDIENTS penalty (capped per severity)
    bad_ing_penalty = 0
    for sev, count in severity_counts.items():
        raw = count * _PENALTY_PER_MATCH[sev]
        capped = min(raw, _PENALTY_CAPS[sev])
        bad_ing_penalty += capped

    # --- Build-up scoring model ---

    # Collect all ingredient names (lowercase) for scanning
    ing_names_lower = [
        (ing.get("name") or "").lower() for ing in ingredients
    ]
    all_ing_text = " ".join(ing_names_lower)

    # 1. Category coverage: 7 pts per healthy category (max 35)
    categories_detected: List[str] = []
    for cat, keywords in HEALTHY_CATEGORIES.items():
        if any(kw in all_ing_text for kw in keywords):
            categories_detected.append(cat)
    cat_score = min(len(categories_detected) * 7, 35)

    # 2. Ingredient quality ratio: proportion matching healthy keywords (max 30)
    all_keywords = [
        kw for kws in HEALTHY_CATEGORIES.values() for kw in kws
    ]
    quality_hits = sum(
        1 for name in ing_names_lower
        if any(kw in name for kw in all_keywords)
    )
    quality_score = round((quality_hits / total) * 30) if total else 0

    # 3. Whole food signals: proportion with freshness markers (max 15)
    whole_hits = sum(
        1 for name in ing_names_lower
        if any(sig in name for sig in WHOLE_FOOD_SIGNALS)
    )
    whole_score = round((whole_hits / total) * 15) if total else 0

    # 4. Processed indicators penalty (max -15, -5 each)
    proc_penalty = 0
    for name in ing_names_lower:
        # Exclude "instant pot" from matching "instant"
        scan = name.replace("instant pot", "")
        if any(ind in scan for ind in PROCESSED_INDICATORS):
            proc_penalty += 5
    proc_penalty = min(proc_penalty, 15)

    # 5. Convenience indicators penalty (max -8, -3 each)
    conv_penalty = 0
    for name in ing_names_lower:
        if any(ind in name for ind in CONVENIENCE_INDICATORS):
            conv_penalty += 3
    conv_penalty = min(conv_penalty, 8)

    # 6. Heavy negatives penalty (max -10, -3 each)
    heavy_penalty = 0
    for name in ing_names_lower:
        if any(ind in name for ind in HEAVY_NEGATIVES):
            heavy_penalty += 3
    heavy_penalty = min(heavy_penalty, 10)

    # 7. Sugar penalty (max -6, -2 each)
    sugar_penalty = 0
    for name in ing_names_lower:
        if "stevia" in name:
            continue
        # Check longer patterns first to avoid double-counting
        if any(kw in name for kw in SUGAR_KEYWORDS):
            sugar_penalty += 2
    sugar_penalty = min(sugar_penalty, 6)

    base = 20
    bonus = cat_score + quality_score + whole_score
    total_penalty = (
        proc_penalty + conv_penalty + heavy_penalty
        + sugar_penalty + bad_ing_penalty
    )
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
    product_info: Dict[str, Dict[str, str]],
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

    products_to_update: List[tuple] = []

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
            ingredients_text = fetch_ingredients_by_name(
                info["description"], info.get("brand", "")
            )

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
                        "UPDATE products SET ingredients_text = ? "
                        "WHERE product_id = ?",
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
    recipe: Dict[str, Any],
    location_id: Optional[str] = None,
) -> Dict[str, Any]:
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


def _empty_cost(recipe: Dict[str, Any], note: str) -> Dict[str, Any]:
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
    recipe: Dict[str, Any],
    location_id: Optional[str],
    conn,
) -> Dict[str, Any]:
    ingredients = recipe.get("ingredients") or []
    servings = max(1, recipe.get("servings") or 1)

    breakdown: List[Dict[str, Any]] = []
    total_cost = 0.0
    priced_count = 0

    for ing in ingredients:
        ing_name = ing.get("name", "")
        pid = ing.get("product_id")

        entry: Dict[str, Any] = {
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


def _fetch_price_by_product_id(conn, product_id: str, location_id: Optional[str]):
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


def _fetch_price_by_name(conn, name: str, location_id: Optional[str]):
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
    entry: Dict[str, Any],
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
    recipe: Dict[str, Any],
    location_id: Optional[str],
    client,
) -> Dict[str, Any]:
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
