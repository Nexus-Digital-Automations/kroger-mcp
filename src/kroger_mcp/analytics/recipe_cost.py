"""Recipe cost estimation.

DB-backed cost estimation using price_history / products data, plus an
API-backed variant that fills unknown prices via Kroger product search.

The public entry point (estimate_recipe_cost) is cached in Redis with a
content-addressed key (shared with recipe_scoring via ``_recipe_content_hash``):
any edit to a recipe's ingredients or servings produces a new key, so writes
self-invalidate. Price drift in unkeyed inputs (price_history rows) expires via
the 1h TTL.
"""

from typing import Any

from kroger_mcp.analytics.recipe_scoring import _recipe_content_hash
from kroger_mcp.cache import cache_read_through

# Drift TTL for inputs the cache key cannot see (price_history / products rows).
_COST_TTL_SECONDS = 3600


def estimate_recipe_cost(
    recipe: dict[str, Any],
    location_id: str | None = None,
    include_spices: bool = False,
) -> dict[str, Any]:
    """Redis-cached wrapper around the DB-only cost estimation.

    Content-addressed key (recipe edits self-invalidate); price drift expires
    via the 1h TTL. estimate_recipe_cost_with_api builds on this cached call.

    ``include_spices`` controls whether spice/seasoning ingredients are summed
    into ``total_cost``/``cost_per_serving`` (default off — spices are shown but
    not counted). It is part of the cache key so toggling re-computes cleanly,
    mirroring the ``:mode`` suffix on the health-score key.
    """
    rid = recipe.get("id") or "adhoc"
    spice_tag = "all" if include_spices else "nospice"
    key = f"recipe:cost:{rid}:{_recipe_content_hash(recipe)}:" f"{location_id or 'any'}:{spice_tag}"
    return cache_read_through(
        key,
        _COST_TTL_SECONDS,
        lambda: _estimate_recipe_cost_uncached(recipe, location_id, include_spices),
    )


def _estimate_recipe_cost_uncached(
    recipe: dict[str, Any],
    location_id: str | None = None,
    include_spices: bool = False,
) -> dict[str, Any]:
    """
    Estimate recipe cost using local price_history / products DB tables only.

    Returns a dict with total_cost, cost_per_serving, breakdown, etc.
    """
    try:
        from .database import get_db_connection

        conn = get_db_connection()
    except Exception:
        return _empty_cost(recipe, "Database unavailable", include_spices)

    try:
        return _estimate_cost_with_conn(recipe, location_id, conn, include_spices)
    finally:
        conn.close()


def _empty_cost(recipe: dict[str, Any], note: str, include_spices: bool = False) -> dict[str, Any]:
    servings = max(1, recipe.get("servings") or 1)
    return {
        "total_cost": None,
        "cost_per_serving": None,
        "currency": "USD",
        "servings": servings,
        "confidence": "low",
        "note": note,
        "breakdown": [],
        "include_spices": include_spices,
    }


def _ingredient_is_spice(ing: dict[str, Any]) -> bool:
    """True when an ingredient reads as a herb/spice/seasoning for cost gating.

    Honors an explicit ``category`` tag, otherwise defers to the shared,
    word-boundary ``is_spice`` name matcher (single source of truth in
    analytics.ingredients) so "salted butter"/"bell pepper" don't mis-match.
    """
    from .ingredients import is_spice

    category = (ing.get("category") or "").strip().lower()
    if category in {
        "spice",
        "spices",
        "herb",
        "herbs",
        "seasoning",
        "seasonings",
        "herbs_spices",
    }:
        return True
    return is_spice(ing.get("name", ""))


def _estimate_cost_with_conn(
    recipe: dict[str, Any],
    location_id: str | None,
    conn,
    include_spices: bool = False,
) -> dict[str, Any]:
    ingredients = recipe.get("ingredients") or []
    servings = max(1, recipe.get("servings") or 1)

    breakdown: list[dict[str, Any]] = []
    total_cost = 0.0
    priced_count = 0  # priced ingredients that count toward the recipe total
    countable_count = 0  # ingredients expected to contribute (non-spice, or all)
    spice_count = 0  # spice/seasoning ingredients shown but excluded

    for ing in ingredients:
        ing_name = ing.get("name", "")
        pid = ing.get("product_id")
        qty = ing.get("quantity") or 1
        is_spice = _ingredient_is_spice(ing)
        counts_toward_total = include_spices or not is_spice
        if is_spice:
            spice_count += 1
        if counts_toward_total:
            countable_count += 1

        entry: dict[str, Any] = {
            "ingredient": ing_name,
            "product_id": pid,
            "matched_description": None,
            "price": None,
            "cost_per_serving": None,
            "is_spice": is_spice,
            "excluded_from_total": is_spice and not include_spices,
            "on_sale": None,
            "regular_price": None,
            "sale_price": None,
            "price_source": "unknown",
            "last_observed": None,
        }

        effective = None
        if pid:
            row = _fetch_price_by_product_id(conn, pid, location_id)
            if row:
                effective, entry = _apply_price_row(row, entry, "exact")
        else:
            row = _fetch_price_by_name(conn, ing_name, location_id)
            if row:
                effective, entry = _apply_price_row(row, entry, "estimated")

        if effective is not None:
            # effective is the price of ONE unit of the linked product; the
            # recipe needs `qty` of them (e.g. "3 onions", "2 cans"), so the
            # ingredient's contribution to the total must scale with it.
            ingredient_cost = effective * qty
            # Per-ingredient cost-per-serving is shown for every priced
            # ingredient, spice or not. Only count it toward the recipe total
            # when it isn't an excluded spice.
            entry["cost_per_serving"] = round(ingredient_cost / servings, 2)
            if counts_toward_total:
                total_cost += ingredient_cost
                priced_count += 1
        else:
            # Row absent or carried no usable price; leave as unknown so the
            # API fallback can retry and confidence stays honest.
            entry["price_source"] = "unknown"

        breakdown.append(entry)

    # Confidence and unknown counts are measured against the *countable* set so
    # excluded spices neither inflate "unknown" nor drag confidence down.
    unknown_count = countable_count - priced_count
    spice_excluded_count = spice_count if not include_spices else 0

    if priced_count == 0:
        final_total = None
        cost_per_serving = None
    else:
        final_total = round(total_cost, 2)
        cost_per_serving = round(total_cost / servings, 2)

    if countable_count == 0:
        confidence = "low"
    elif priced_count == countable_count:
        confidence = "high"
    elif priced_count >= countable_count / 2:
        confidence = "medium"
    else:
        confidence = "low"

    notes = []
    if unknown_count > 0:
        notes.append(f"{unknown_count} ingredient(s) have no price data")
    if spice_excluded_count > 0:
        notes.append(
            f"{spice_excluded_count} spice/seasoning(s) shown but not counted "
            "toward per-serving cost"
        )
    note_str = "; ".join(notes) if notes else None

    return {
        "total_cost": final_total,
        "cost_per_serving": cost_per_serving,
        "currency": "USD",
        "servings": servings,
        "confidence": confidence,
        "note": note_str,
        "breakdown": breakdown,
        "include_spices": include_spices,
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

    entry["matched_description"] = row["product_description"]
    entry["price"] = effective
    entry["on_sale"] = on_sale
    entry["regular_price"] = regular
    entry["sale_price"] = sale
    entry["price_source"] = source
    entry["last_observed"] = row["observed_at"]
    return effective, entry


# ---------------------------------------------------------------------------
# API-backed cost (for analyze action)
# ---------------------------------------------------------------------------


def estimate_recipe_cost_with_api(
    recipe: dict[str, Any],
    location_id: str | None,
    client,
    include_spices: bool = False,
) -> dict[str, Any]:
    """
    Same as estimate_recipe_cost but fills in unknown prices via Kroger API search.

    Args:
        recipe: Recipe dict.
        location_id: Preferred Kroger location ID.
        client: Authenticated KrogerAPI client.
        include_spices: Fold spice/seasoning ingredients into the recipe total
            (default off — spices are shown but excluded from per-serving cost).

    Returns:
        Same structure as estimate_recipe_cost, with api_search entries in breakdown.
    """
    # Start with DB estimate (same spice mode so the seeded flags match)
    result = estimate_recipe_cost(recipe, location_id, include_spices)
    api_fallback_note = None
    servings = result["servings"]

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
                    entry["cost_per_serving"] = round(effective / servings, 2)
                    entry["on_sale"] = on_sale
                    entry["regular_price"] = regular
                    entry["sale_price"] = promo
                    entry["price_source"] = "api_search"
                    entry["matched_description"] = item.get("description")
        except Exception:
            api_fallback_note = "API search unavailable for some ingredients"

    # Recompute totals, excluding spices that don't count toward the total
    # (the is_spice / excluded_from_total flags were seeded by the DB pass).
    breakdown = result["breakdown"]
    countable = [e for e in breakdown if not e.get("excluded_from_total")]
    contributing = [e for e in countable if e["price"] is not None]
    countable_count = len(countable)
    priced_count = len(contributing)

    if priced_count == 0:
        result["total_cost"] = None
        result["cost_per_serving"] = None
    else:
        total = sum(e["price"] for e in contributing)
        result["total_cost"] = round(total, 2)
        result["cost_per_serving"] = round(total / servings, 2)

    if countable_count == 0:
        result["confidence"] = "low"
    elif priced_count == countable_count:
        result["confidence"] = "high"
    elif priced_count >= countable_count / 2:
        result["confidence"] = "medium"
    else:
        result["confidence"] = "low"

    if api_fallback_note:
        result["api_fallback_note"] = api_fallback_note

    return result
