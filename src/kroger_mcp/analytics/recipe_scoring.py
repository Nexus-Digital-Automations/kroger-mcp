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
    ],
    "lean_protein": [
        "chicken", "turkey", "salmon", "tuna", "egg", "lentil",
        "chickpea", "black bean", "kidney bean", "tofu", "tempeh",
    ],
    "whole_grain": [
        "brown rice", "quinoa", "oats", "whole wheat", "whole grain",
        "farro", "barley", "bulgur",
    ],
    "healthy_fat": [
        "olive oil", "avocado", "almond", "walnut", "cashew",
        "flaxseed", "chia", "hemp seed",
    ],
    "herbs_spices": [
        "basil", "oregano", "thyme", "rosemary", "cilantro", "parsley",
        "mint", "dill", "cumin", "turmeric", "ginger", "cinnamon",
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
    Calculate a heuristic health score (0-100) for a recipe.

    Args:
        recipe: Recipe dict with "ingredients" list.
        names_only: If True, only scan ingredient names (no DB product lookups).

    Returns:
        Dict with score, grade, confidence, flags, categories_detected, etc.
    """
    from .ingredients import check_product_safety

    ingredients = recipe.get("ingredients") or []
    total = len(ingredients)

    if total == 0:
        return {
            "score": 100,
            "grade": "A",
            "confidence": "high",
            "flags": [],
            "categories_detected": [],
            "bonus_applied": 0,
            "linked_ingredients": 0,
            "total_ingredients": 0,
        }

    # Batch-load linked products if not names_only
    product_info: Dict[str, Dict[str, str]] = {}
    if not names_only:
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
                        f"SELECT product_id, description, brand "
                        f"FROM products WHERE product_id IN ({placeholders})",
                        linked_ids,
                    ).fetchall()
                    for row in rows:
                        product_info[row["product_id"]] = {
                            "description": row["description"] or "",
                            "brand": row["brand"] or "",
                        }
                finally:
                    conn.close()
            except Exception:
                pass

    # Accumulate penalties and flags
    linked_count = 0
    severity_counts: Dict[str, int] = {"critical": 0, "warning": 0, "watch": 0}
    flags: List[Dict[str, Any]] = []

    for ing in ingredients:
        pid = ing.get("product_id")
        ing_name = ing.get("name", "")

        if pid and pid in product_info:
            linked_count += 1
            scan_text = product_info[pid]["description"]
            if product_info[pid]["brand"]:
                scan_text = product_info[pid]["brand"] + " " + scan_text
        elif pid:
            # product_id present but not in DB — count as linked (best effort)
            linked_count += 1
            scan_text = ing_name
        else:
            scan_text = ing_name

        result = check_product_safety(scan_text)
        for match in result.matches:
            sev = match.severity.value  # "critical" | "warning" | "watch"
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            flags.append({
                "ingredient": ing_name,
                "flag": match.ingredient_name,
                "severity": sev,
                "reason": match.reason,
            })

    # Compute total penalty (capped per severity)
    total_penalty = 0
    for sev, count in severity_counts.items():
        raw = count * _PENALTY_PER_MATCH[sev]
        capped = min(raw, _PENALTY_CAPS[sev])
        total_penalty += capped

    # >50% unlinked penalty
    unlinked_ratio = (total - linked_count) / total
    if unlinked_ratio > 0.5:
        total_penalty += 5

    # Bonus: healthy category matching (on ingredient names always)
    categories_detected: List[str] = []
    all_ing_text = " ".join(
        (ing.get("name") or "").lower() for ing in ingredients
    )
    for cat, keywords in HEALTHY_CATEGORIES.items():
        if any(kw in all_ing_text for kw in keywords):
            categories_detected.append(cat)

    bonus = min(len(categories_detected) * 3, 15)

    score = max(0, min(100, 100 - total_penalty + bonus))

    # Confidence
    if linked_count == total:
        confidence = "high"
    elif linked_count >= total / 2:
        confidence = "medium"
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
        "total_ingredients": total,
    }


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
