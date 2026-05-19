"""
USDA FoodData Central API integration.

Fetches real ingredient lists for branded food products by UPC barcode.
Used by recipe health scoring to get actual label ingredients
(e.g., "ENRICHED FLOUR, WATER, SUGAR, SOYBEAN OIL, SALT...")
instead of just product names.

API docs: https://fdc.nal.usda.gov/api-guide/
Rate limit: 1,000 requests/hour per IP.
Auth: free data.gov API key via env var USDA_API_KEY.
"""

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv

    # Walk up to find .env in the project root
    _here = Path(__file__).resolve()
    for _parent in _here.parents:
        _env = _parent / ".env"
        if _env.exists():
            load_dotenv(_env)
            break
except ImportError:
    pass


FDC_API_BASE = "https://api.nal.usda.gov/fdc/v1"


def _get_api_key() -> str | None:
    """Return the USDA API key from environment, or None."""
    key = os.environ.get("USDA_API_KEY", "").strip()
    return key if key else None


def _fdc_request(
    endpoint: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Make a FoodData Central API request.

    Returns parsed JSON dict, or empty dict on any failure.
    Never raises — callers should not be blocked by API issues.
    """
    api_key = _get_api_key()
    if not api_key:
        return {}

    separator = "&" if "?" in endpoint else "?"
    url = f"{FDC_API_BASE}{endpoint}{separator}api_key={api_key}"
    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body else None

    req = Request(url, data=data, headers=headers, method=method)
    try:
        # URL is built from FDC_API_BASE (const https://) + internal endpoint
        # path. bandit B310's SSRF concern (file://, ftp://) does not apply.
        with urlopen(req, timeout=15) as resp:  # nosec B310
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {}


def fetch_ingredients_by_upc(upc: str) -> str | None:
    """
    Search FoodData Central for a branded product by UPC and return
    its ingredient statement text.

    Args:
        upc: The product UPC/GTIN barcode (e.g., "0001111060903").

    Returns:
        The full ingredient list string, or None if not found.
    """
    if not upc or not _get_api_key():
        return None

    # Strip leading zeros for flexible matching — FDC stores varying formats
    upc_stripped = upc.lstrip("0")
    if not upc_stripped:
        return None

    result = _fdc_request(
        "/foods/search",
        method="POST",
        body={
            "query": upc,
            "dataType": ["Branded"],
            "pageSize": 3,
        },
    )

    foods = result.get("foods") or []

    # Try exact UPC match first
    for food in foods:
        gtin = (food.get("gtinUpc") or "").lstrip("0")
        if gtin == upc_stripped:
            ingredients = food.get("ingredients")
            if ingredients and isinstance(ingredients, str):
                return ingredients.strip()

    # Fall back to first result with ingredients
    for food in foods:
        ingredients = food.get("ingredients")
        if ingredients and isinstance(ingredients, str):
            return ingredients.strip()

    return None


def fetch_ingredients_by_name(
    product_name: str,
    brand: str = "",
) -> str | None:
    """
    Search FoodData Central by product name/description and return
    the ingredient statement from the best match.

    Fallback when UPC search yields no results — Kroger internal
    product IDs often don't match USDA UPC formats.

    Args:
        product_name: Product description (e.g., "Cream of Celery Soup").
        brand: Optional brand name to improve matching.

    Returns:
        The full ingredient list string, or None if not found.
    """
    if not product_name or not _get_api_key():
        return None

    query = f"{brand} {product_name}".strip() if brand else product_name

    result = _fdc_request(
        "/foods/search",
        method="POST",
        body={
            "query": query,
            "dataType": ["Branded"],
            "pageSize": 3,
        },
    )

    foods = result.get("foods") or []

    for food in foods:
        ingredients = food.get("ingredients")
        if ingredients and isinstance(ingredients, str):
            return ingredients.strip()

    return None


def fetch_food_details(fdc_id: int) -> dict[str, Any] | None:
    """
    Get full food details by FDC ID.

    Returns the food detail dict, or None on failure.
    """
    if not fdc_id or not _get_api_key():
        return None

    result = _fdc_request(f"/food/{fdc_id}")
    return result if result else None
