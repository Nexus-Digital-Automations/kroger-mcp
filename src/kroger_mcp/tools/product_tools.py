"""
Product search and management tools for Kroger MCP server.
"""

import asyncio
import functools
from datetime import datetime
from typing import Any, Literal

import requests
from fastmcp import Context, Image
from pydantic import Field

from ..analytics.database import get_db_connection, get_db_cursor
from ..analytics.deals import record_price_observation
from ..analytics.favorites import get_all_favorite_product_ids
from ..analytics.ingredients import check_product_safety, score_to_status
from ..analytics.safety import (
    BlockMode,
    get_all_blocked_product_ids,
    get_all_safe_product_ids,
    get_block_mode,
    get_disabled_ingredients,
    is_filtering_enabled,
)
from .shared import (
    format_currency,
    get_client_credentials_client,
    get_default_zip_code,
    get_preferred_location_id,
    set_preferred_location_id,
)


def _cache_usda_ingredients(product: dict[str, Any]) -> None:
    """
    If product has a UPC and no cached USDA ingredient text,
    fetch from USDA FoodData Central and store in the local DB.
    """
    pid = product.get("product_id")
    upc = product.get("upc")
    if not pid or not upc:
        return

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT ingredients_text FROM products WHERE product_id = ?",
            (pid,),
        ).fetchone()
        if row and row["ingredients_text"]:
            return  # Already cached

        from ..analytics.usda import fetch_ingredients_by_name, fetch_ingredients_by_upc

        ingredients_text = fetch_ingredients_by_upc(upc)
        if not ingredients_text:
            ingredients_text = fetch_ingredients_by_name(
                product.get("description", ""),
                product.get("brand", ""),
            )
        if not ingredients_text:
            return

        conn.execute(
            "INSERT INTO products (product_id, upc, description, brand, "
            "ingredients_text, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(product_id) DO UPDATE SET "
            "upc = COALESCE(excluded.upc, products.upc), "
            "ingredients_text = excluded.ingredients_text, "
            "updated_at = excluded.updated_at",
            (
                pid,
                upc,
                product.get("description"),
                product.get("brand"),
                ingredients_text,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def is_whole_food_eligible(
    description: str,
    brand: str | None = None,
    disabled_ingredients: set | None = None,
) -> dict[str, Any]:
    """
    Check if product qualifies as whole food.

    Uses existing safety filter - product must:
    1. Pass safety check (no CRITICAL/WARNING ingredients)
    2. Have UNKNOWN or SAFE status
    3. Optional: Low WATCH ingredient count (<3)
    """
    safety_result = check_product_safety(
        description=description,
        brand=brand,
        disabled_ingredients=disabled_ingredients,
    )

    if safety_result.highest_severity in ["critical", "warning"]:
        return {
            "eligible": False,
            "safety_status": safety_result.highest_severity.upper(),
            "reason": f"Contains {safety_result.highest_severity} ingredients",
            "matches": [
                {"ingredient": m.ingredient_name, "severity": m.severity}
                for m in safety_result.matches
            ],
        }

    if not safety_result.has_concerns:
        return {
            "eligible": True,
            "safety_status": "SAFE",
            "reason": "No concerning ingredients detected",
            "matches": [],
        }

    watch_count = len([m for m in safety_result.matches if m.severity == "watch"])
    if watch_count <= 2:
        return {
            "eligible": True,
            "safety_status": "WATCH",
            "reason": f"Minimal processing markers ({watch_count} watch-level ingredients)",
            "matches": [
                {"ingredient": m.ingredient_name, "severity": m.severity}
                for m in safety_result.matches
            ],
        }

    return {
        "eligible": False,
        "safety_status": "WATCH",
        "reason": f"Too many processing markers ({watch_count} watch-level ingredients)",
        "matches": [
            {"ingredient": m.ingredient_name, "severity": m.severity} for m in safety_result.matches
        ],
    }


def register_tools(mcp):
    """Register product-related tools with the FastMCP server."""

    @mcp.tool()
    async def products(
        action: Literal[
            "search",
            "get_details",
            "get_images",
            "search_by_id",
            "add_to_whole_foods",
            "get_whole_foods_catalog",
            "scan_for_whole_foods",
        ] = Field(
            description=(
                "search — batch via search_terms=[...] (max 10). "
                "search_by_id — batch via product_ids (max 20). "
                "Other: get_details|get_images|add_to_whole_foods|get_whole_foods_catalog|scan_for_whole_foods"
            )
        ),
        search_term: str | None = Field(
            default=None,
            description="Search term e.g. milk",
        ),
        search_terms: list[str] | None = Field(
            default=None,
            description="Batch search terms (max 10)",
        ),
        product_id: str | None = Field(
            default=None,
            description="Product ID",
        ),
        product_ids: list[str] | None = Field(
            default=None,
            description="Product IDs for batch (max 20)",
        ),
        location_id: str | None = Field(
            default=None,
            description="Store location ID",
        ),
        limit: int | None = Field(
            default=10,
            description="Max results to return",
        ),
        fulfillment: str | None = Field(
            default=None,
            description="csp|delivery|pickup",
        ),
        brand: str | None = Field(
            default=None,
            description="Filter by brand name",
        ),
        prioritize_favorites: bool | None = Field(
            default=True,
            description="Boost favorites to top",
        ),
        perspective: str | None = Field(
            default="front",
            description="front|back|left|right",
        ),
        description: str | None = Field(
            default=None,
            description="Product description",
        ),
        verify_safety: bool | None = Field(
            default=True,
            description="Verify safety before adding",
        ),
        include_unavailable: bool | None = Field(
            default=False,
            description="Include unavailable products",
        ),
        category: str | None = Field(
            default=None,
            description="produce|dairy|meat|bakery|frozen",
        ),
        auto_add: bool | None = Field(
            default=False,
            description="Auto-add qualifying products",
        ),
        ctx: Context = None,
    ) -> Any:
        """Product search with batch support and safety integration.

        search — single or batch (search_terms=[...], max 10). Auto-checks safety.
        search_by_id — look up products by ID (batch: product_ids, max 20).
        get_details — full product info including pricing and availability.
        Whole foods: add_to_whole_foods, get_whole_foods_catalog, scan_for_whole_foods.
        Images: get_images (perspective: front|back|left|right).

        Safety filtering is ON by default (verify_safety=True). Favorites boosted
        in results when prioritize_favorites=True (default).
        """

        # ---- Shared helpers ----

        def _get_location(loc_id):
            if not loc_id:
                loc_id = get_preferred_location_id()
            if not loc_id:
                # Auto-detect: search near default zip and cache the first result
                zip_code = get_default_zip_code()
                try:
                    client = get_client_credentials_client()
                    results = client.locations.search(
                        zip_code=zip_code, radius_in_miles=10, limit=1
                    )
                    if results:
                        loc_id = results[0]["locationId"]
                        set_preferred_location_id(loc_id)
                except Exception:
                    pass
            if not loc_id:
                return None, {
                    "success": False,
                    "error": (
                        "No location_id provided, no preferred location set, and "
                        f"auto-detect failed (searched zip: {zip_code}). "
                        "Call location(action='set_preferred', zip_code='YOUR_ZIP') "
                        "to set a store manually."
                    ),
                }
            return loc_id, None

        def _get_safety_data():
            filtering = is_filtering_enabled()
            safe_ids = set()
            blocked_ids = set()
            disabled = set()
            bmode = BlockMode.SOFT
            if filtering:
                try:
                    safe_ids = get_all_safe_product_ids()
                    blocked_ids = get_all_blocked_product_ids()
                    disabled = get_disabled_ingredients()
                    bmode = get_block_mode()
                except Exception:
                    pass
            return filtering, safe_ids, blocked_ids, disabled, bmode

        match action:
            case "search":
                loc_id, err = await asyncio.to_thread(_get_location, location_id)
                if err:
                    return err

                terms = []
                if search_terms:
                    terms = search_terms
                elif search_term:
                    terms = [search_term]
                else:
                    return {"success": False, "error": "search_term or search_terms is required"}

                is_batch = len(terms) > 1

                if len(terms) > 10:
                    return {
                        "success": False,
                        "error": f"Too many search terms ({len(terms)}). Maximum is 10.",
                    }

                if ctx:
                    if is_batch:
                        await ctx.info(f"Searching {len(terms)} terms at location {loc_id}")
                    else:
                        await ctx.info(f"Searching for '{terms[0]}' at location {loc_id}")

                client = await asyncio.to_thread(get_client_credentials_client)
                _prio_favs = prioritize_favorites if prioritize_favorites is not None else True

                favorite_ids = set()
                if _prio_favs:
                    try:
                        favorite_ids = get_all_favorite_product_ids()
                    except Exception:
                        pass

                filtering, safe_ids, blocked_ids, disabled, bmode = _get_safety_data()
                _limit = limit if limit is not None else 10

                def format_product(product: dict) -> dict:
                    fp = {
                        "product_id": product.get("productId"),
                        "upc": product.get("upc"),
                        "description": product.get("description"),
                        "brand": product.get("brand"),
                        "categories": product.get("categories", []),
                        "country_origin": product.get("countryOrigin"),
                        "temperature": product.get("temperature", {}),
                    }
                    if "items" in product and product["items"]:
                        item = product["items"][0]
                        fp["item"] = {
                            "size": item.get("size"),
                            "sold_by": item.get("soldBy"),
                            "inventory": item.get("inventory", {}),
                            "fulfillment": item.get("fulfillment", {}),
                        }
                        if "price" in item:
                            price = item["price"]
                            fp["pricing"] = {
                                "regular_price": price.get("regular"),
                                "sale_price": price.get("promo"),
                                "regular_per_unit": price.get("regularPerUnitEstimate"),
                                "formatted_regular": format_currency(price.get("regular")),
                                "formatted_sale": format_currency(price.get("promo")),
                                "on_sale": price.get("promo") is not None
                                and price.get("promo") < price.get("regular", float("inf")),
                            }
                    if "aisleLocations" in product:
                        fp["aisle_locations"] = [
                            {
                                "description": a.get("description"),
                                "number": a.get("number"),
                                "side": a.get("side"),
                                "shelf_number": a.get("shelfNumber"),
                            }
                            for a in product["aisleLocations"]
                        ]
                    if "images" in product and product["images"]:
                        fp["images"] = [
                            {
                                "perspective": img.get("perspective"),
                                "url": img["sizes"][0].get("url") if img.get("sizes") else None,
                                "size": img["sizes"][0].get("size") if img.get("sizes") else None,
                            }
                            for img in product["images"]
                            if img.get("sizes")
                        ]
                    pid = fp.get("product_id", "")
                    desc = fp.get("description", "")
                    if filtering:
                        if pid in safe_ids:
                            fp["is_safe_listed"] = True
                            fp["is_blocked"] = False
                            fp["safety_status"] = "safe"
                            fp["safety_score"] = None
                            fp["safety_grade"] = None
                            fp["positive_attributes"] = []
                            fp["flagged_ingredients"] = []
                        elif pid in blocked_ids:
                            fp["is_safe_listed"] = False
                            fp["is_blocked"] = True
                            fp["safety_status"] = "blocked"
                            fp["safety_score"] = None
                            fp["safety_grade"] = None
                            fp["positive_attributes"] = []
                            fp["flagged_ingredients"] = []
                        else:
                            fp["is_safe_listed"] = False
                            fp["is_blocked"] = False
                            safety_result = check_product_safety(
                                description=desc,
                                brand=fp.get("brand"),
                                disabled_ingredients=disabled,
                            )
                            fp["safety_status"] = score_to_status(safety_result.score)
                            fp["safety_score"] = safety_result.score
                            fp["safety_grade"] = safety_result.grade
                            fp["positive_attributes"] = [
                                {
                                    "attribute": a.attribute_name,
                                    "bonus": a.bonus,
                                    "benefit": a.benefit,
                                }
                                for a in safety_result.positive_attributes
                            ]
                            fp["flagged_ingredients"] = [
                                {
                                    "ingredient": m.ingredient_name,
                                    "severity": m.severity.value,
                                    "reason": m.reason,
                                    "matched_text": m.matched_text,
                                }
                                for m in safety_result.matches
                            ]
                    else:
                        fp["is_safe_listed"] = False
                        fp["is_blocked"] = False
                        fp["safety_status"] = "acceptable"
                        fp["safety_score"] = 60
                        fp["safety_grade"] = "C"
                        fp["positive_attributes"] = []
                        fp["flagged_ingredients"] = []
                    return fp

                def mark_and_sort(plist):
                    fav_count = 0
                    safety_counts = {
                        "safe": 0,
                        "blocked": 0,
                        "excellent": 0,
                        "good": 0,
                        "acceptable": 0,
                        "poor": 0,
                        "avoid": 0,
                    }
                    for p in plist:
                        is_fav = p.get("product_id") in favorite_ids
                        p["is_favorite"] = is_fav
                        if is_fav:
                            fav_count += 1
                        status = p.get("safety_status", "acceptable")
                        if status in safety_counts:
                            safety_counts[status] += 1

                    if filtering and bmode == BlockMode.HARD:
                        plist = [
                            p for p in plist if p.get("safety_status") not in ("blocked", "avoid")
                        ]

                    def sort_key(p):
                        status = p.get("safety_status", "acceptable")
                        is_fav = p.get("is_favorite", False)
                        is_safe = p.get("is_safe_listed", False)
                        if is_safe:
                            return 0
                        elif is_fav:
                            return 1
                        elif status == "excellent":
                            return 2
                        elif status == "good":
                            return 3
                        elif status == "acceptable":
                            return 4
                        elif status == "poor":
                            return 5
                        elif status == "avoid":
                            return 6
                        elif status == "blocked":
                            return 7
                        return 8

                    if _prio_favs or filtering:
                        plist = sorted(plist, key=sort_key)
                    return plist, fav_count, safety_counts

                async def search_single(term: str):
                    try:
                        prods = await asyncio.to_thread(
                            functools.partial(
                                client.product.search_products,
                                term=term,
                                location_id=loc_id,
                                limit=_limit,
                                fulfillment=fulfillment,
                                brand=brand,
                            )
                        )
                        if not prods or "data" not in prods or not prods["data"]:
                            return (
                                term,
                                {"count": 0, "favorites_count": 0, "safety_counts": {}, "data": []},
                            )

                        formatted = [format_product(p) for p in prods["data"]]
                        formatted, fav_count, safety_counts = mark_and_sort(formatted)

                        for product in formatted:
                            try:
                                pricing = product.get("pricing", {})
                                if pricing and loc_id:
                                    record_price_observation(
                                        product_id=product["product_id"],
                                        regular_price=pricing.get("regular_price"),
                                        sale_price=pricing.get("sale_price"),
                                        location_id=loc_id,
                                        source="search",
                                    )
                            except Exception:
                                pass
                            # Cache UPC + USDA ingredient data for health scoring
                            try:
                                _cache_usda_ingredients(product)
                            except Exception:
                                pass

                        return (
                            term,
                            {
                                "count": len(formatted),
                                "favorites_count": fav_count,
                                "safety_counts": safety_counts,
                                "data": formatted,
                            },
                        )
                    except Exception as e:
                        return (term, {"error": str(e), "count": 0, "data": []})

                try:
                    tasks = [search_single(t) for t in terms]
                    results_list = await asyncio.gather(*tasks)

                    if is_batch:
                        results = {}
                        errors = {}
                        total_results = 0
                        total_favorites = 0
                        total_safety = {
                            "safe": 0,
                            "blocked": 0,
                            "excellent": 0,
                            "good": 0,
                            "acceptable": 0,
                            "poor": 0,
                            "avoid": 0,
                        }

                        for term, result in results_list:
                            if "error" in result:
                                errors[term] = result["error"]
                            else:
                                results[term] = result
                                total_results += result["count"]
                                total_favorites += result["favorites_count"]
                                for k, v in result.get("safety_counts", {}).items():
                                    if k in total_safety:
                                        total_safety[k] += v

                        if ctx:
                            flagged = total_safety.get("avoid", 0) + total_safety.get("poor", 0)
                            await ctx.info(
                                f"Found {total_results} products ({total_favorites} favorites, {flagged} flagged)"
                            )

                        return {
                            "success": len(errors) < len(terms),
                            "location_id": loc_id,
                            "terms_searched": len(terms),
                            "total_results": total_results,
                            "total_favorites": total_favorites,
                            "safety_counts": total_safety,
                            "filtering_enabled": filtering,
                            "results": results,
                            "errors": errors if errors else None,
                        }
                    else:
                        term, result = results_list[0]
                        if "error" in result:
                            return {"success": False, "error": result["error"], "data": []}

                        safety_counts = result.get("safety_counts", {})
                        flagged = safety_counts.get("avoid", 0) + safety_counts.get("poor", 0)

                        if ctx:
                            await ctx.info(
                                f"Found {result['count']} products ({result['favorites_count']} favorites, {flagged} flagged)"
                            )

                        return {
                            "success": True,
                            "search_params": {
                                "search_term": term,
                                "location_id": loc_id,
                                "limit": _limit,
                                "fulfillment": fulfillment,
                                "brand": brand,
                                "prioritize_favorites": _prio_favs,
                            },
                            "count": result["count"],
                            "favorites_count": result["favorites_count"],
                            "safety_counts": safety_counts,
                            "filtering_enabled": filtering,
                            "data": result["data"],
                        }
                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error searching products: {str(e)}")
                    return {"success": False, "error": str(e), "data": []}

            case "get_details":
                loc_id, err = await asyncio.to_thread(_get_location, location_id)
                if err:
                    return err

                ids = []
                if product_ids:
                    ids = product_ids
                elif product_id:
                    ids = [product_id]
                else:
                    return {"success": False, "error": "product_id or product_ids is required"}

                is_batch = len(ids) > 1

                if len(ids) > 20:
                    return {
                        "success": False,
                        "error": f"Too many product IDs ({len(ids)}). Maximum is 20.",
                    }

                if ctx:
                    if is_batch:
                        await ctx.info(f"Getting details for {len(ids)} products")
                    else:
                        await ctx.info(f"Getting details for product {ids[0]}")

                client = await asyncio.to_thread(get_client_credentials_client)

                def format_details(product: dict) -> dict:
                    result = {
                        "product_id": product.get("productId"),
                        "upc": product.get("upc"),
                        "description": product.get("description"),
                        "brand": product.get("brand"),
                        "categories": product.get("categories", []),
                        "country_origin": product.get("countryOrigin"),
                        "temperature": product.get("temperature", {}),
                        "location_id": loc_id,
                    }
                    if "items" in product and product["items"]:
                        item = product["items"][0]
                        result["item_details"] = {
                            "size": item.get("size"),
                            "sold_by": item.get("soldBy"),
                            "inventory": item.get("inventory", {}),
                            "fulfillment": item.get("fulfillment", {}),
                        }
                        if "price" in item:
                            price = item["price"]
                            result["pricing"] = {
                                "regular_price": price.get("regular"),
                                "sale_price": price.get("promo"),
                                "regular_per_unit": price.get("regularPerUnitEstimate"),
                                "formatted_regular": format_currency(price.get("regular")),
                                "formatted_sale": format_currency(price.get("promo")),
                                "on_sale": price.get("promo") is not None
                                and price.get("promo") < price.get("regular", float("inf")),
                                "savings": (
                                    price.get("regular", 0)
                                    - price.get("promo", price.get("regular", 0))
                                    if price.get("promo")
                                    else 0
                                ),
                            }
                    if "aisleLocations" in product:
                        result["aisle_locations"] = [
                            {
                                "description": a.get("description"),
                                "aisle_number": a.get("number"),
                                "side": a.get("side"),
                                "shelf_number": a.get("shelfNumber"),
                            }
                            for a in product["aisleLocations"]
                        ]
                    if "images" in product and product["images"]:
                        result["images"] = [
                            {
                                "perspective": img.get("perspective"),
                                "sizes": [
                                    {"size": s.get("size"), "url": s.get("url")}
                                    for s in img.get("sizes", [])
                                ],
                            }
                            for img in product["images"]
                        ]
                    return result

                async def fetch_single(pid: str):
                    try:
                        product_details = await asyncio.to_thread(
                            functools.partial(
                                client.product.get_product,
                                product_id=pid,
                                location_id=loc_id,
                            )
                        )
                        if not product_details or "data" not in product_details:
                            return (pid, {"error": f"Product {pid} not found"})
                        result = format_details(product_details["data"])
                        try:
                            pricing = result.get("pricing", {})
                            if pricing and loc_id:
                                record_price_observation(
                                    product_id=pid,
                                    regular_price=pricing.get("regular_price"),
                                    sale_price=pricing.get("sale_price"),
                                    location_id=loc_id,
                                    source="details",
                                )
                        except Exception:
                            pass
                        return (pid, result)
                    except Exception as e:
                        return (pid, {"error": str(e)})

                try:
                    tasks = [fetch_single(pid) for pid in ids]
                    results_list = await asyncio.gather(*tasks)

                    if is_batch:
                        results = {}
                        errors = {}
                        for pid, result in results_list:
                            if "error" in result:
                                errors[pid] = result["error"]
                            else:
                                results[pid] = result
                        if ctx:
                            await ctx.info(
                                f"Retrieved {len(results)} products, {len(errors)} errors"
                            )
                        return {
                            "success": len(errors) < len(ids),
                            "location_id": loc_id,
                            "count": len(results),
                            "results": results,
                            "errors": errors if errors else None,
                        }
                    else:
                        pid, result = results_list[0]
                        if "error" in result:
                            return {"success": False, "message": result["error"]}
                        return {"success": True, **result}
                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error getting product details: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "get_images":
                if not product_id:
                    return {"success": False, "error": "product_id is required"}

                loc_id, err = await asyncio.to_thread(_get_location, location_id)
                if err:
                    return err

                _perspective = perspective if perspective is not None else "front"

                if ctx:
                    await ctx.info(f"Fetching images for product {product_id} at location {loc_id}")

                client = await asyncio.to_thread(get_client_credentials_client)

                try:
                    product_details = await asyncio.to_thread(
                        functools.partial(
                            client.product.get_product,
                            product_id=product_id,
                            location_id=loc_id,
                        )
                    )
                    if not product_details or "data" not in product_details:
                        return {"success": False, "message": f"Product {product_id} not found"}

                    product = product_details["data"]

                    if "images" not in product or not product["images"]:
                        return {
                            "success": False,
                            "message": f"No images available for product {product_id}",
                        }

                    perspective_image = None
                    available_perspectives = []
                    size_preference = ["large", "xlarge", "medium", "small", "thumbnail"]

                    for img_data in product["images"]:
                        img_perspective = img_data.get("perspective", "unknown")
                        available_perspectives.append(img_perspective)

                        if img_perspective != _perspective:
                            continue
                        if not img_data.get("sizes"):
                            continue

                        available_sizes = {
                            size.get("size"): size.get("url")
                            for size in img_data.get("sizes", [])
                            if size.get("size") and size.get("url")
                        }

                        img_url = None
                        for size in size_preference:
                            if size in available_sizes:
                                img_url = available_sizes[size]
                                break

                        if img_url:
                            try:
                                if ctx:
                                    await ctx.info(
                                        f"Downloading {_perspective} image from {img_url}"
                                    )
                                response = await asyncio.to_thread(
                                    requests.get, img_url, timeout=10
                                )
                                response.raise_for_status()
                                perspective_image = Image(data=response.content, format="jpeg")
                                break
                            except Exception as e:
                                if ctx:
                                    await ctx.warning(
                                        f"Failed to download {_perspective} image: {str(e)}"
                                    )

                    if not perspective_image:
                        available_str = (
                            ", ".join(available_perspectives) if available_perspectives else "none"
                        )
                        return {
                            "success": False,
                            "message": f"No image found for perspective '{_perspective}'. Available perspectives: {available_str}",
                        }

                    return perspective_image

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error getting product images: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "search_by_id":
                if not product_id:
                    return {"success": False, "error": "product_id is required"}

                loc_id, err = await asyncio.to_thread(_get_location, location_id)
                if err:
                    return err

                _prio_favs = prioritize_favorites if prioritize_favorites is not None else True

                if ctx:
                    await ctx.info(
                        f"Searching for products with ID '{product_id}' at location {loc_id}"
                    )

                client = await asyncio.to_thread(get_client_credentials_client)
                filtering, safe_ids, blocked_ids, disabled, bmode = _get_safety_data()

                try:
                    prods = await asyncio.to_thread(
                        functools.partial(
                            client.product.search_products,
                            product_id=product_id,
                            location_id=loc_id,
                        )
                    )
                    if not prods or "data" not in prods or not prods["data"]:
                        return {
                            "success": False,
                            "message": f"No products found with ID '{product_id}'",
                            "data": [],
                        }

                    formatted_products = []
                    for product in prods["data"]:
                        pid = product.get("productId", "")
                        desc = product.get("description", "")
                        prd_brand = product.get("brand")
                        fp = {
                            "product_id": pid,
                            "upc": product.get("upc"),
                            "description": desc,
                            "brand": prd_brand,
                            "categories": product.get("categories", []),
                        }
                        if (
                            "items" in product
                            and product["items"]
                            and "price" in product["items"][0]
                        ):
                            price = product["items"][0]["price"]
                            fp["pricing"] = {
                                "regular_price": price.get("regular"),
                                "sale_price": price.get("promo"),
                                "formatted_regular": format_currency(price.get("regular")),
                                "formatted_sale": format_currency(price.get("promo")),
                            }
                        if filtering:
                            if pid in safe_ids:
                                fp["is_safe_listed"] = True
                                fp["is_blocked"] = False
                                fp["safety_status"] = "safe"
                                fp["safety_score"] = None
                                fp["safety_grade"] = None
                                fp["positive_attributes"] = []
                                fp["flagged_ingredients"] = []
                            elif pid in blocked_ids:
                                fp["is_safe_listed"] = False
                                fp["is_blocked"] = True
                                fp["safety_status"] = "blocked"
                                fp["safety_score"] = None
                                fp["safety_grade"] = None
                                fp["positive_attributes"] = []
                                fp["flagged_ingredients"] = []
                            else:
                                fp["is_safe_listed"] = False
                                fp["is_blocked"] = False
                                safety_result = check_product_safety(
                                    description=desc, brand=prd_brand, disabled_ingredients=disabled
                                )
                                fp["safety_status"] = score_to_status(safety_result.score)
                                fp["safety_score"] = safety_result.score
                                fp["safety_grade"] = safety_result.grade
                                fp["positive_attributes"] = [
                                    {
                                        "attribute": a.attribute_name,
                                        "bonus": a.bonus,
                                        "benefit": a.benefit,
                                    }
                                    for a in safety_result.positive_attributes
                                ]
                                fp["flagged_ingredients"] = [
                                    {
                                        "ingredient": m.ingredient_name,
                                        "severity": m.severity.value,
                                        "reason": m.reason,
                                        "matched_text": m.matched_text,
                                    }
                                    for m in safety_result.matches
                                ]
                        else:
                            fp["is_safe_listed"] = False
                            fp["is_blocked"] = False
                            fp["safety_status"] = "acceptable"
                            fp["safety_score"] = 60
                            fp["safety_grade"] = "C"
                            fp["positive_attributes"] = []
                            fp["flagged_ingredients"] = []
                        formatted_products.append(fp)

                    favorite_ids = set()
                    if _prio_favs:
                        try:
                            favorite_ids = get_all_favorite_product_ids()
                        except Exception:
                            pass

                    favorites_count = 0
                    safety_counts = {
                        "safe": 0,
                        "blocked": 0,
                        "excellent": 0,
                        "good": 0,
                        "acceptable": 0,
                        "poor": 0,
                        "avoid": 0,
                    }
                    for product in formatted_products:
                        is_fav = product.get("product_id") in favorite_ids
                        product["is_favorite"] = is_fav
                        if is_fav:
                            favorites_count += 1
                        status = product.get("safety_status", "acceptable")
                        if status in safety_counts:
                            safety_counts[status] += 1

                    if filtering and bmode == BlockMode.HARD:
                        formatted_products = [
                            p
                            for p in formatted_products
                            if p.get("safety_status") not in ("blocked", "avoid")
                        ]

                    if _prio_favs or filtering:

                        def sort_key(p):
                            status = p.get("safety_status", "acceptable")
                            is_fav = p.get("is_favorite", False)
                            is_safe = p.get("is_safe_listed", False)
                            if is_safe:
                                return 0
                            elif is_fav:
                                return 1
                            elif status == "excellent":
                                return 2
                            elif status == "good":
                                return 3
                            elif status == "acceptable":
                                return 4
                            elif status == "poor":
                                return 5
                            elif status == "avoid":
                                return 6
                            elif status == "blocked":
                                return 7
                            return 8

                        formatted_products = sorted(formatted_products, key=sort_key)

                    flagged = safety_counts.get("avoid", 0) + safety_counts.get("poor", 0)
                    if ctx:
                        await ctx.info(
                            f"Found {len(formatted_products)} products ({favorites_count} favorites, {flagged} flagged)"
                        )

                    return {
                        "success": True,
                        "search_params": {
                            "product_id": product_id,
                            "location_id": loc_id,
                            "prioritize_favorites": _prio_favs,
                        },
                        "count": len(formatted_products),
                        "favorites_count": favorites_count,
                        "safety_counts": safety_counts,
                        "filtering_enabled": filtering,
                        "data": formatted_products,
                    }
                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error searching products by ID: {str(e)}")
                    return {"success": False, "error": str(e), "data": []}

            case "add_to_whole_foods":
                if not product_id:
                    return {"success": False, "error": "product_id is required"}

                prod_desc = description
                if not prod_desc:
                    loc_id = get_preferred_location_id()
                    if loc_id:
                        try:
                            client = await asyncio.to_thread(get_client_credentials_client)
                            product_data = await asyncio.to_thread(
                                functools.partial(
                                    client.get_product,
                                    product_id=product_id,
                                    location_id=loc_id,
                                )
                            )
                            if product_data and product_data.get("data"):
                                prod_desc = product_data["data"].get("description")
                        except Exception:
                            pass

                safety_status = "UNKNOWN"
                eligibility_result = None
                _verify = verify_safety if verify_safety is not None else True

                if _verify and prod_desc:
                    disabled = get_disabled_ingredients()
                    eligibility_result = is_whole_food_eligible(
                        description=prod_desc, disabled_ingredients=disabled
                    )
                    if not eligibility_result["eligible"]:
                        return {
                            "success": False,
                            "product_id": product_id,
                            "description": prod_desc,
                            "error": eligibility_result["reason"],
                            "safety_status": eligibility_result["safety_status"],
                            "matches": eligibility_result["matches"],
                        }
                    safety_status = eligibility_result["safety_status"]

                with get_db_cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO whole_foods_catalog
                        (product_id, description, added_by, safety_status, last_verified_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(product_id) DO UPDATE SET
                            description = excluded.description,
                            safety_status = excluded.safety_status,
                            last_verified_at = excluded.last_verified_at
                        """,
                        (
                            product_id,
                            prod_desc,
                            "manual",
                            safety_status,
                            datetime.now().isoformat(),
                        ),
                    )

                return {
                    "success": True,
                    "product_id": product_id,
                    "description": prod_desc,
                    "safety_status": safety_status,
                    "message": "Added to whole foods catalog",
                    "eligibility": eligibility_result if eligibility_result else None,
                }

            case "get_whole_foods_catalog":
                _include_unavailable = (
                    include_unavailable if include_unavailable is not None else False
                )
                _limit = limit if limit is not None else 100

                conn = get_db_connection()
                try:
                    availability_filter = (
                        "" if _include_unavailable else "WHERE is_currently_available = 1"
                    )
                    cursor = conn.execute(
                        f"""
                        SELECT
                            product_id,
                            description,
                            brand,
                            added_at,
                            added_by,
                            safety_status,
                            processing_level,
                            notes,
                            last_verified_at,
                            is_currently_available
                        FROM whole_foods_catalog
                        {availability_filter}
                        ORDER BY added_at DESC
                        LIMIT ?
                        """,
                        (_limit,),
                    )
                    catalog_products = [dict(row) for row in cursor.fetchall()]
                    return {
                        "success": True,
                        "products": catalog_products,
                        "total": len(catalog_products),
                        "include_unavailable": _include_unavailable,
                    }
                finally:
                    conn.close()

            case "scan_for_whole_foods":
                if not category:
                    return {"success": False, "error": "category is required"}

                loc_id, err = await asyncio.to_thread(_get_location, location_id)
                if err:
                    return err

                _limit = limit if limit is not None else 20
                _auto_add = auto_add if auto_add is not None else False

                category_searches = {
                    "produce": "vegetables",
                    "dairy": "milk",
                    "meat": "chicken breast",
                    "bakery": "bread",
                    "frozen": "frozen vegetables",
                }
                search_term_wf = category_searches.get(category.lower(), category)

                if ctx:
                    await ctx.info(f"Scanning for whole foods in category: {category}")

                try:
                    client = await asyncio.to_thread(get_client_credentials_client)
                    search_result = await asyncio.to_thread(
                        functools.partial(
                            client.search_products,
                            term=search_term_wf,
                            location_id=loc_id,
                            limit=_limit,
                        )
                    )
                    if not search_result or not search_result.get("data"):
                        return {"success": False, "error": "Search failed or returned no results"}
                    scan_products = search_result.get("data", [])
                except Exception as e:
                    return {"success": False, "error": f"Search failed: {str(e)}"}

                disabled = get_disabled_ingredients()
                qualifying_products = []
                rejected_products = []

                for product in scan_products:
                    prod_desc = product.get("description")
                    prod_brand = product.get("brand")
                    pid = product.get("product_id")

                    if not prod_desc:
                        continue

                    eligibility = is_whole_food_eligible(
                        description=prod_desc, brand=prod_brand, disabled_ingredients=disabled
                    )

                    result = {
                        "product_id": pid,
                        "description": prod_desc,
                        "brand": prod_brand,
                        "eligible": eligibility["eligible"],
                        "safety_status": eligibility["safety_status"],
                        "reason": eligibility["reason"],
                        "matches": eligibility["matches"],
                    }

                    if eligibility["eligible"]:
                        qualifying_products.append(result)
                        if _auto_add:
                            try:
                                with get_db_cursor() as cursor:
                                    cursor.execute(
                                        """
                                        INSERT INTO whole_foods_catalog
                                        (product_id, description, brand, added_by, safety_status, last_verified_at)
                                        VALUES (?, ?, ?, ?, ?, ?)
                                        ON CONFLICT(product_id) DO UPDATE SET
                                            safety_status = excluded.safety_status,
                                            last_verified_at = excluded.last_verified_at
                                        """,
                                        (
                                            pid,
                                            prod_desc,
                                            prod_brand,
                                            "auto_scan",
                                            eligibility["safety_status"],
                                            datetime.now().isoformat(),
                                        ),
                                    )
                            except Exception:
                                pass
                    else:
                        rejected_products.append(result)

                return {
                    "success": True,
                    "category": category,
                    "qualifying_products": qualifying_products,
                    "rejected_products": rejected_products if ctx else [],
                    "summary": {
                        "scanned": len(scan_products),
                        "qualifying": len(qualifying_products),
                        "rejected": len(rejected_products),
                        "auto_added": len(qualifying_products) if _auto_add else 0,
                    },
                }

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
