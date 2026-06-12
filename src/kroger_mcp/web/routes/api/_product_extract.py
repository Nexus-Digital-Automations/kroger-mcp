"""Kroger product-record normalisation shared by the products API.

Pure functions: parse size strings into per-unit prices, choose product
images, and flatten Kroger API objects/dicts into the UI's product shape.
Split out of products.py to keep that module within the size budget.
"""

from __future__ import annotations

import re

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
