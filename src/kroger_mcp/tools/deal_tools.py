"""
Deal discovery and price tracking tools for Kroger MCP server.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

from fastmcp import Context
from pydantic import Field

from .shared import get_client_credentials_client, get_preferred_location_id
from ..analytics.deals import get_price_statistics, score_deal_quality
from ..analytics.database import get_db_cursor, get_db_connection
from ..analytics.favorites import get_all_favorite_product_ids
from ..analytics.pantry import get_low_inventory_items
from ..analytics.statistics import get_recent_purchases


# Category search mappings
CATEGORY_SEARCHES = {
    "dairy": ["milk", "cheese", "yogurt", "butter"],
    "meat": ["chicken", "beef", "pork", "turkey"],
    "produce": ["fruits", "vegetables", "salad"],
    "bakery": ["bread", "bagels", "rolls"],
    "frozen": ["frozen meals", "ice cream", "pizza"],
    "beverages": ["soda", "juice", "coffee", "tea"],
}


def register_tools(mcp):
    """Register deal-related tools with the FastMCP server"""

    @mcp.tool()
    async def deals(
        action: Literal[
            "find",
            "add_to_watchlist",
            "get_price_history",
            "scan_watchlist",
            "get_latest_scan",
        ] = Field(
            description=(
                "Action: 'find' - search for products on sale, "
                "'add_to_watchlist' - track a product for price drops, "
                "'get_price_history' - view price trends for a product, "
                "'scan_watchlist' - check tracked items for current deals, "
                "'get_latest_scan' - view results from last automated scan"
            )
        ),
        search_term: Optional[str] = Field(
            default=None,
            description="Search term e.g. 'milk' (for find; searches popular categories if not provided)",
        ),
        category: Optional[str] = Field(
            default=None,
            description="Category: 'dairy', 'meat', 'produce', 'bakery', 'frozen', 'beverages' (for find)",
        ),
        min_savings_percent: Optional[float] = Field(
            default=10.0,
            description="Minimum discount percentage (for find)",
        ),
        sort_by: Optional[str] = Field(
            default="savings_percent",
            description="Sort by: 'savings_percent', 'savings_amount', 'price' (for find)",
        ),
        limit: Optional[int] = Field(
            default=20,
            description="Maximum number of deals to return (for find)",
        ),
        location_id: Optional[str] = Field(
            default=None,
            description="Store location ID (uses preferred if not provided)",
        ),
        product_id: Optional[str] = Field(
            default=None,
            description="Single product ID (for add_to_watchlist or get_price_history)",
        ),
        product_ids: Optional[List[str]] = Field(
            default=None,
            description="List of product IDs for batch watchlist add (max 30; for add_to_watchlist)",
        ),
        description: Optional[str] = Field(
            default=None,
            description="Product description for watchlist (for add_to_watchlist)",
        ),
        target_price: Optional[float] = Field(
            default=None,
            description="Alert when price reaches this target (for add_to_watchlist)",
        ),
        priority: Optional[int] = Field(
            default=1,
            description="Priority: 1=low, 2=medium, 3=high (for add_to_watchlist)",
        ),
        days: Optional[int] = Field(
            default=30,
            description="Days of price history to retrieve (for get_price_history)",
        ),
        include_favorites: Optional[bool] = Field(
            default=True,
            description="Include favorite list items in scan (for scan_watchlist)",
        ),
        include_pantry: Optional[bool] = Field(
            default=True,
            description="Include pantry items in scan (for scan_watchlist)",
        ),
        include_recent_purchases: Optional[bool] = Field(
            default=True,
            description="Include recently purchased items in scan (for scan_watchlist)",
        ),
        max_items: Optional[int] = Field(
            default=50,
            description="Maximum items to scan (for scan_watchlist)",
        ),
        mark_as_viewed: Optional[bool] = Field(
            default=False,
            description="Mark results as viewed (for get_latest_scan)",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Deal discovery and price tracking operations."""
        match action:
            case "find":
                if not location_id:
                    location_id = get_preferred_location_id()
                    if not location_id:
                        return {
                            "success": False,
                            "error": "No location_id provided and no preferred location set. "
                            "Use location(action='set_preferred') first.",
                        }

                search_queries = []
                if search_term:
                    search_queries = [search_term]
                elif category and category.lower() in CATEGORY_SEARCHES:
                    search_queries = CATEGORY_SEARCHES[category.lower()]
                else:
                    search_queries = ["milk", "bread", "chicken"]

                if ctx:
                    await ctx.info(
                        f"Searching for deals: {', '.join(search_queries)} "
                        f"(min {min_savings_percent}% off)"
                    )

                favorite_ids = set()
                try:
                    favorite_ids = get_all_favorite_product_ids()
                except Exception:
                    pass

                pantry_items = {}
                try:
                    low_items = get_low_inventory_items(threshold=50)
                    pantry_items = {item["product_id"]: item for item in low_items}
                except Exception:
                    pass

                all_deals = []
                categories_scanned = set()

                for query in search_queries:
                    try:
                        client = get_client_credentials_client()
                        search_response = client.search_products(
                            term=query,
                            location_id=location_id,
                            limit=50,
                        )

                        if not search_response or "data" not in search_response:
                            continue

                        products = search_response.get("data", [])

                        for product in products:
                            pricing = product.get("pricing", {})
                            if not pricing.get("on_sale"):
                                continue

                            regular = pricing.get("regular_price", 0)
                            sale = pricing.get("sale_price", 0)
                            if not regular or not sale or sale >= regular:
                                continue

                            savings_amount = regular - sale
                            savings_percent = (savings_amount / regular) * 100

                            if savings_percent < (min_savings_percent or 10.0):
                                continue

                            deal = {
                                "product_id": product.get("product_id"),
                                "description": product.get("description"),
                                "brand": product.get("brand"),
                                "regular_price": regular,
                                "sale_price": sale,
                                "savings_amount": round(savings_amount, 2),
                                "savings_percent": round(savings_percent, 1),
                                "is_favorite": product.get("product_id") in favorite_ids,
                                "is_in_pantry": product.get("product_id") in pantry_items,
                                "pantry_level": pantry_items.get(
                                    product.get("product_id"), {}
                                ).get("level_percent", 100),
                            }

                            try:
                                price_stats = get_price_statistics(
                                    product.get("product_id"),
                                    days=30,
                                    location_id=location_id,
                                )
                                if price_stats.get("has_data"):
                                    deal["price_trend"] = price_stats.get("recommendation")
                                    deal["recommendation"] = price_stats.get(
                                        "recommendation_text"
                                    )
                            except Exception:
                                price_stats = None

                            try:
                                quality = score_deal_quality(
                                    {"pricing": pricing, **deal},
                                    price_stats,
                                )
                                deal["quality_score"] = quality["quality_score"]
                                deal["quality_label"] = quality["quality_label"]
                                deal["urgency"] = quality["urgency"]
                            except Exception:
                                pass

                            all_deals.append(deal)

                        if product.get("categories"):
                            categories_scanned.update(product["categories"])

                    except Exception as e:
                        if ctx:
                            await ctx.warn(f"Error searching '{query}': {str(e)}")
                        continue

                seen_ids = set()
                unique_deals = []
                for deal in all_deals:
                    if deal["product_id"] not in seen_ids:
                        seen_ids.add(deal["product_id"])
                        unique_deals.append(deal)

                sort_key = sort_by or "savings_percent"
                if sort_key == "savings_percent":
                    unique_deals.sort(key=lambda x: x["savings_percent"], reverse=True)
                elif sort_key == "savings_amount":
                    unique_deals.sort(key=lambda x: x["savings_amount"], reverse=True)
                elif sort_key == "price":
                    unique_deals.sort(key=lambda x: x["sale_price"])

                unique_deals = unique_deals[: limit or 20]

                total_savings = sum(d["savings_amount"] for d in unique_deals)
                avg_savings_percent = (
                    sum(d["savings_percent"] for d in unique_deals) / len(unique_deals)
                    if unique_deals
                    else 0
                )

                return {
                    "success": True,
                    "deals": unique_deals,
                    "summary": {
                        "total_deals_found": len(unique_deals),
                        "total_savings_available": round(total_savings, 2),
                        "avg_savings_percent": round(avg_savings_percent, 1),
                        "categories_scanned": list(categories_scanned)[:10],
                        "search_queries": search_queries,
                    },
                }

            case "add_to_watchlist":
                if product_ids is not None:
                    actual_ids = product_ids
                elif product_id is not None:
                    actual_ids = [product_id]
                else:
                    return {
                        "success": False,
                        "error": "product_id or product_ids is required",
                    }

                is_batch = len(actual_ids) > 1

                if len(actual_ids) > 30:
                    return {
                        "success": False,
                        "error": "Maximum 30 products per batch request",
                    }

                loc_id = location_id or get_preferred_location_id()
                priority_labels = {1: "low", 2: "medium", 3: "high"}
                pri = priority or 1

                try:
                    results = {}
                    for pid in actual_ids:
                        try:
                            current_price = None
                            current_on_sale = False
                            prod_description = description

                            if loc_id:
                                try:
                                    client = get_client_credentials_client()
                                    product_response = client.get_product(
                                        product_id=pid, location_id=loc_id
                                    )
                                    if product_response and "data" in product_response:
                                        product_data = product_response.get("data", {})
                                        pricing = product_data.get("pricing", {})
                                        current_price = pricing.get(
                                            "sale_price"
                                        ) or pricing.get("regular_price")
                                        current_on_sale = pricing.get("on_sale", False)
                                        if not prod_description:
                                            prod_description = product_data.get("description")
                                except Exception:
                                    pass

                            with get_db_cursor() as cursor:
                                cursor.execute(
                                    """
                                    INSERT INTO deal_watchlist
                                    (product_id, description, target_price, priority, best_price_seen, best_price_date)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(product_id) DO UPDATE SET
                                        description = excluded.description,
                                        target_price = excluded.target_price,
                                        priority = excluded.priority
                                    """,
                                    (
                                        pid,
                                        prod_description,
                                        target_price,
                                        pri,
                                        current_price,
                                        datetime.now().isoformat() if current_price else None,
                                    ),
                                )

                            results[pid] = {
                                "success": True,
                                "product_id": pid,
                                "description": prod_description,
                                "current_price": current_price,
                                "current_on_sale": current_on_sale,
                                "target_price": target_price,
                                "priority": priority_labels.get(pri, "unknown"),
                                "message": f"Added to watchlist with {priority_labels.get(pri, 'unknown')} priority",
                            }
                        except Exception as e:
                            results[pid] = {
                                "success": False,
                                "error": f"Failed to add {pid} to watchlist: {str(e)}",
                            }

                    if is_batch:
                        success_count = sum(1 for r in results.values() if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(actual_ids),
                                "successful": success_count,
                                "failed": len(actual_ids) - success_count,
                                "priority": priority_labels.get(pri, "unknown"),
                            },
                        }
                    else:
                        return results[actual_ids[0]]

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to add to watchlist: {str(e)}",
                    }

            case "get_price_history":
                if not product_id:
                    return {"success": False, "error": "product_id is required"}

                loc_id = location_id or get_preferred_location_id()
                days_val = days or 30

                stats = get_price_statistics(product_id, days=days_val, location_id=loc_id)

                if not stats.get("has_data"):
                    return {
                        "success": False,
                        "product_id": product_id,
                        "error": "No price history available for this product",
                    }

                conn = get_db_connection()
                try:
                    since_date = (datetime.now() - timedelta(days=days_val)).isoformat()
                    location_filter = "AND location_id = ?" if loc_id else ""
                    params = [product_id, since_date]
                    if loc_id:
                        params.append(loc_id)

                    cursor = conn.execute(
                        f"""
                        SELECT
                            DATE(observed_at) as date,
                            AVG(COALESCE(sale_price, regular_price)) as avg_price,
                            MAX(on_sale) as on_sale,
                            MAX(savings_percent) as max_savings
                        FROM price_history
                        WHERE product_id = ?
                        AND observed_at > ?
                        {location_filter}
                        GROUP BY DATE(observed_at)
                        ORDER BY date DESC
                        """,
                        params,
                    )

                    timeline = [
                        {
                            "date": row["date"],
                            "price": round(row["avg_price"], 2),
                            "on_sale": bool(row["on_sale"]),
                            "savings_percent": (
                                round(row["max_savings"], 1) if row["max_savings"] else 0
                            ),
                        }
                        for row in cursor.fetchall()
                    ]

                finally:
                    conn.close()

                prod_description = None
                try:
                    if loc_id:
                        client = get_client_credentials_client()
                        product_response = client.get_product(
                            product_id=product_id, location_id=loc_id
                        )
                        if product_response and "data" in product_response:
                            prod_description = product_response.get("data", {}).get(
                                "description"
                            )
                except Exception:
                    pass

                return {
                    "success": True,
                    "product_id": product_id,
                    "description": prod_description,
                    "current_price": stats["current_price"],
                    "current_on_sale": stats["current_on_sale"],
                    "statistics": {
                        "avg_price_30d": stats["avg_price_30d"],
                        "lowest_price_30d": stats["lowest_price_30d"],
                        "highest_price_30d": stats["highest_price_30d"],
                        "times_on_sale": stats["times_on_sale"],
                        "avg_savings_when_on_sale": stats["avg_savings_when_on_sale"],
                        "current_vs_avg": (
                            f"{((stats['current_price'] - stats['avg_price_30d']) / stats['avg_price_30d'] * 100):+.1f}%"
                        ),
                        "trend": stats["trend"],
                        "recommendation": stats["recommendation_text"],
                    },
                    "price_timeline": timeline[:30],
                    "observations_count": stats["observations_count"],
                }

            case "scan_watchlist":
                loc_id = location_id or get_preferred_location_id()
                if not loc_id:
                    return {
                        "success": False,
                        "error": "No location_id provided and no preferred location set.",
                    }

                if ctx:
                    await ctx.info("Building watchlist from your tracked items...")

                watchlist = []

                conn = get_db_connection()
                try:
                    cursor = conn.execute(
                        """
                        SELECT product_id, description, target_price, priority
                        FROM deal_watchlist
                        ORDER BY priority DESC, last_checked_at ASC
                        """
                    )
                    for row in cursor.fetchall():
                        watchlist.append(
                            {
                                "product_id": row["product_id"],
                                "description": row["description"],
                                "target_price": row["target_price"],
                                "source": "watchlist",
                                "priority": row["priority"],
                            }
                        )
                finally:
                    conn.close()

                if include_favorites if include_favorites is not None else True:
                    try:
                        favorite_ids = get_all_favorite_product_ids()
                        for fav_id in favorite_ids:
                            if not any(w["product_id"] == fav_id for w in watchlist):
                                watchlist.append(
                                    {
                                        "product_id": fav_id,
                                        "source": "favorites",
                                        "priority": 2,
                                    }
                                )
                    except Exception:
                        pass

                if include_pantry if include_pantry is not None else True:
                    try:
                        low_items = get_low_inventory_items(threshold=25)
                        for item in low_items:
                            pid = item["product_id"]
                            if not any(w["product_id"] == pid for w in watchlist):
                                watchlist.append(
                                    {
                                        "product_id": pid,
                                        "description": item.get("description"),
                                        "source": "pantry",
                                        "priority": 3,
                                    }
                                )
                    except Exception:
                        pass

                if include_recent_purchases if include_recent_purchases is not None else True:
                    try:
                        recent = get_recent_purchases(days=30, limit=20)
                        for purchase in recent:
                            pid = purchase["product_id"]
                            if not any(w["product_id"] == pid for w in watchlist):
                                watchlist.append(
                                    {
                                        "product_id": pid,
                                        "description": purchase.get("description"),
                                        "source": "recent_purchase",
                                        "priority": 1,
                                    }
                                )
                    except Exception:
                        pass

                watchlist.sort(key=lambda x: x.get("priority", 0), reverse=True)
                watchlist = watchlist[: max_items or 50]

                if ctx:
                    await ctx.info(
                        f"Scanning {len(watchlist)} items for deals "
                        f"(from {', '.join(set(w['source'] for w in watchlist))})..."
                    )

                deal_items = []
                for item in watchlist:
                    try:
                        client = get_client_credentials_client()
                        product_response = client.get_product(
                            product_id=item["product_id"], location_id=loc_id
                        )

                        if not product_response or "data" not in product_response:
                            continue

                        data = product_response.get("data", {})
                        pricing = data.get("pricing", {})

                        if not pricing.get("on_sale"):
                            continue

                        target_met = False
                        if item.get("target_price"):
                            current_price = pricing.get("sale_price") or pricing.get(
                                "regular_price"
                            )
                            target_met = current_price <= item["target_price"]

                        deal = {
                            "product_id": item["product_id"],
                            "description": data.get("description") or item.get("description"),
                            "brand": data.get("brand"),
                            "regular_price": pricing.get("regular_price"),
                            "sale_price": pricing.get("sale_price"),
                            "savings_amount": (
                                pricing.get("regular_price") - pricing.get("sale_price")
                                if pricing.get("regular_price") and pricing.get("sale_price")
                                else 0
                            ),
                            "savings_percent": round(
                                (
                                    (
                                        pricing.get("regular_price")
                                        - pricing.get("sale_price")
                                    )
                                    / pricing.get("regular_price")
                                    * 100
                                )
                                if pricing.get("regular_price") and pricing.get("sale_price")
                                else 0,
                                1,
                            ),
                            "source": item["source"],
                            "target_price": item.get("target_price"),
                            "target_met": target_met,
                        }

                        deal_items.append(deal)

                    except Exception as e:
                        if ctx:
                            await ctx.warn(f"Error checking {item['product_id']}: {str(e)}")
                        continue

                deal_items.sort(key=lambda x: x["savings_percent"], reverse=True)
                total_savings = sum(d["savings_amount"] for d in deal_items)

                return {
                    "success": True,
                    "deals": deal_items,
                    "summary": {
                        "items_scanned": len(watchlist),
                        "deals_found": len(deal_items),
                        "total_savings_available": round(total_savings, 2),
                        "sources": list(set(w["source"] for w in watchlist)),
                    },
                }

            case "get_latest_scan":
                conn = get_db_connection()
                try:
                    cursor = conn.execute(
                        """
                        SELECT scan_date, scan_time, COUNT(*) as deal_count
                        FROM deal_scan_results
                        GROUP BY scan_date
                        ORDER BY scan_date DESC
                        LIMIT 1
                        """
                    )
                    latest = cursor.fetchone()

                    if not latest:
                        return {
                            "success": True,
                            "message": "No scans found yet. First scan runs Monday 9 AM.",
                            "deals": [],
                            "summary": {
                                "scan_date": None,
                                "deal_count": 0,
                                "total_savings_available": 0,
                                "unviewed_deals": 0,
                            },
                        }

                    cursor = conn.execute(
                        """
                        SELECT product_id, description, regular_price, sale_price,
                               savings_amount, viewed
                        FROM deal_scan_results
                        WHERE scan_date = ?
                        ORDER BY savings_amount DESC
                        """,
                        (latest["scan_date"],),
                    )

                    deal_items = [dict(row) for row in cursor.fetchall()]

                    if mark_as_viewed:
                        conn.execute(
                            """
                            UPDATE deal_scan_results
                            SET viewed = 1
                            WHERE scan_date = ?
                            """,
                            (latest["scan_date"],),
                        )
                        conn.commit()

                    return {
                        "success": True,
                        "scan_date": latest["scan_date"],
                        "scan_time": latest["scan_time"],
                        "deal_count": latest["deal_count"],
                        "deals": deal_items,
                        "summary": {
                            "total_savings_available": round(
                                sum(d["savings_amount"] for d in deal_items), 2
                            ),
                            "unviewed_deals": sum(1 for d in deal_items if not d["viewed"]),
                            "message": (
                                f"Scanned on {latest['scan_date']}, "
                                f"found {latest['deal_count']} deals"
                            ),
                        },
                    }

                finally:
                    conn.close()

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
