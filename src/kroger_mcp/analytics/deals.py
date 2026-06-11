"""
Deal discovery and price tracking analytics.
"""

import statistics
from datetime import datetime, timedelta
from typing import Any

from .database import get_db_connection, get_db_cursor


def record_price_observation(
    product_id: str,
    regular_price: float | None,
    sale_price: float | None,
    location_id: str,
    source: str = "search",
) -> None:
    """
    Record a price observation in price_history table.

    Automatically called from:
    - search_products() → Record all search results
    - get_product_details() → Record detailed fetches
    - add_to_cart() → Record at purchase time

    Args:
        product_id: Product identifier
        regular_price: Regular/base price
        sale_price: Promotional/sale price (None if not on sale)
        location_id: Store location
        source: Where observation came from
    """
    if not product_id or not location_id:
        return

    # Calculate sale metrics
    on_sale = (
        sale_price is not None
        and regular_price is not None
        and sale_price < regular_price
    )
    if sale_price is not None and regular_price is not None and on_sale:
        savings_amount = regular_price - sale_price
    else:
        savings_amount = 0.0
    savings_percent = (
        (savings_amount / regular_price * 100)
        if on_sale and regular_price and regular_price > 0
        else 0.0
    )

    observed_at = datetime.now().isoformat()

    # Use UPSERT to avoid exact duplicates within same hour
    # (same product, location, hour = single record)
    with get_db_cursor() as cursor:
        # Ensure product exists (FK requirement for price_history)
        cursor.execute(
            "INSERT OR IGNORE INTO products (product_id) VALUES (?)",
            (product_id,),
        )

        # Check if we have a recent observation (within last hour)
        hour_ago = (datetime.now() - timedelta(hours=1)).isoformat()
        cursor.execute(
            """
            SELECT id FROM price_history
            WHERE product_id = ?
            AND location_id = ?
            AND observed_at > ?
            AND on_sale = ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (product_id, location_id, hour_ago, int(on_sale)),
        )
        existing = cursor.fetchone()

        if existing:
            # Update existing observation
            cursor.execute(
                """
                UPDATE price_history
                SET regular_price = ?,
                    sale_price = ?,
                    savings_amount = ?,
                    savings_percent = ?,
                    observed_at = ?,
                    source = ?
                WHERE id = ?
                """,
                (
                    regular_price,
                    sale_price,
                    savings_amount,
                    savings_percent,
                    observed_at,
                    source,
                    existing["id"],
                ),
            )
        else:
            # Insert new observation
            cursor.execute(
                """
                INSERT INTO price_history
                (product_id, regular_price, sale_price, on_sale,
                 savings_amount, savings_percent, location_id,
                 observed_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    regular_price,
                    sale_price,
                    int(on_sale),
                    savings_amount,
                    savings_percent,
                    location_id,
                    observed_at,
                    source,
                ),
            )


def record_price_observations(
    observations: list[dict[str, Any]],
) -> None:
    """Record many price observations in a single transaction.

    Batched equivalent of :func:`record_price_observation`, used to keep N
    per-product DB writes off the request path (callers fire-and-forget this
    on a background thread). Each observation dict carries the same fields as
    the single-row function's arguments::

        {
            "product_id": str,
            "regular_price": float | None,
            "sale_price": float | None,
            "location_id": str,
            "source": str,  # optional, defaults to "search"
        }

    The per-row UPSERT semantics are identical to
    :func:`record_price_observation` (dedupe within the same hour on
    product/location/on_sale); the only difference is that every row shares
    one ``get_db_cursor()`` transaction instead of opening a connection each.
    Observations missing ``product_id`` or ``location_id`` are skipped, and an
    empty list is a no-op.
    """
    if not observations:
        return

    with get_db_cursor() as cursor:
        for obs in observations:
            product_id = obs.get("product_id")
            location_id = obs.get("location_id")
            if not product_id or not location_id:
                continue

            regular_price = obs.get("regular_price")
            sale_price = obs.get("sale_price")
            source = obs.get("source", "search")

            on_sale = (
                sale_price is not None
                and regular_price is not None
                and sale_price < regular_price
            )
            if sale_price is not None and regular_price is not None and on_sale:
                savings_amount = regular_price - sale_price
            else:
                savings_amount = 0.0
            savings_percent = (
                (savings_amount / regular_price * 100)
                if on_sale and regular_price and regular_price > 0
                else 0.0
            )

            observed_at = datetime.now().isoformat()

            # Ensure product exists (FK requirement for price_history)
            cursor.execute(
                "INSERT OR IGNORE INTO products (product_id) VALUES (?)",
                (product_id,),
            )

            # Dedupe within the last hour (same product/location/on_sale).
            hour_ago = (datetime.now() - timedelta(hours=1)).isoformat()
            cursor.execute(
                """
                SELECT id FROM price_history
                WHERE product_id = ?
                AND location_id = ?
                AND observed_at > ?
                AND on_sale = ?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (product_id, location_id, hour_ago, int(on_sale)),
            )
            existing = cursor.fetchone()

            if existing:
                cursor.execute(
                    """
                    UPDATE price_history
                    SET regular_price = ?,
                        sale_price = ?,
                        savings_amount = ?,
                        savings_percent = ?,
                        observed_at = ?,
                        source = ?
                    WHERE id = ?
                    """,
                    (
                        regular_price,
                        sale_price,
                        savings_amount,
                        savings_percent,
                        observed_at,
                        source,
                        existing["id"],
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO price_history
                    (product_id, regular_price, sale_price, on_sale,
                     savings_amount, savings_percent, location_id,
                     observed_at, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product_id,
                        regular_price,
                        sale_price,
                        int(on_sale),
                        savings_amount,
                        savings_percent,
                        location_id,
                        observed_at,
                        source,
                    ),
                )


def get_price_statistics(
    product_id: str, days: int = 30, location_id: str | None = None
) -> dict[str, Any]:
    """
    Analyze price history for a product.

    Args:
        product_id: Product to analyze
        days: Number of days to look back
        location_id: Optional location filter

    Returns:
        {
            "current_price": float,
            "avg_price_30d": float,
            "lowest_price_30d": float,
            "highest_price_30d": float,
            "times_on_sale": int,
            "avg_savings_when_on_sale": float,
            "trend": "rising" | "falling" | "stable",
            "recommendation": "excellent_deal" | "good_deal" | "fair_price" | "high_price"
        }
    """
    conn = get_db_connection()
    try:
        since_date = (datetime.now() - timedelta(days=days)).isoformat()

        # Build query with optional location filter
        location_filter = "AND location_id = ?" if location_id else ""
        params = [product_id, since_date]
        if location_id:
            params.append(location_id)

        cursor = conn.execute(
            f"""
            SELECT
                regular_price,
                sale_price,
                on_sale,
                savings_amount,
                observed_at
            FROM price_history
            WHERE product_id = ?
            AND observed_at > ?
            {location_filter}
            ORDER BY observed_at DESC
            """,
            params,
        )

        observations = cursor.fetchall()

        if not observations:
            return {
                "has_data": False,
                "message": "No price history available for this product",
            }

        # Get current price (most recent observation)
        current = observations[0]
        current_price = current["sale_price"] or current["regular_price"]

        # Calculate statistics
        all_prices = []
        sale_observations = []

        for obs in observations:
            price = obs["sale_price"] or obs["regular_price"]
            if price:
                all_prices.append(price)
            if obs["on_sale"]:
                sale_observations.append(obs)

        if not all_prices:
            return {
                "has_data": False,
                "message": "No valid price data",
            }

        avg_price = statistics.mean(all_prices)
        lowest_price = min(all_prices)
        highest_price = max(all_prices)
        times_on_sale = len(sale_observations)

        avg_savings_when_on_sale = (
            statistics.mean([obs["savings_amount"] for obs in sale_observations])
            if sale_observations
            else 0.0
        )

        # Trend analysis (simple: compare first half vs second half)
        if len(all_prices) >= 4:
            midpoint = len(all_prices) // 2
            first_half_avg = statistics.mean(all_prices[:midpoint])
            second_half_avg = statistics.mean(all_prices[midpoint:])
            price_change = ((second_half_avg - first_half_avg) / first_half_avg) * 100

            if price_change > 5:
                trend = "rising"
            elif price_change < -5:
                trend = "falling"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"

        # Generate recommendation
        if current_price <= lowest_price * 1.05:
            recommendation = "excellent_deal"
        elif current_price <= avg_price * 0.9:
            recommendation = "good_deal"
        elif current_price <= avg_price * 1.1:
            recommendation = "fair_price"
        else:
            recommendation = "high_price"

        # Build recommendation text
        rec_texts = {
            "excellent_deal": f"Excellent! At or near lowest price seen (${lowest_price:.2f})",
            "good_deal": f"Good deal - below average price of ${avg_price:.2f}",
            "fair_price": f"Fair price - near average of ${avg_price:.2f}",
            "high_price": f"High - typically around ${avg_price:.2f}",
        }

        return {
            "has_data": True,
            "current_price": current_price,
            "current_on_sale": bool(current["on_sale"]),
            "avg_price_30d": round(avg_price, 2),
            "lowest_price_30d": lowest_price,
            "highest_price_30d": highest_price,
            "times_on_sale": times_on_sale,
            "avg_savings_when_on_sale": round(avg_savings_when_on_sale, 2),
            "trend": trend,
            "recommendation": recommendation,
            "recommendation_text": rec_texts[recommendation],
            "observations_count": len(observations),
            "days_tracked": days,
        }

    finally:
        conn.close()


def calculate_cart_savings(cart_items: list[dict]) -> dict[str, Any]:
    """
    Calculate total savings for cart items.

    Args:
        cart_items: List of cart items with pricing info

    Returns:
        {
            "total_regular_price": 125.50,
            "total_sale_price": 98.75,
            "total_savings": 26.75,
            "savings_percent": 21.3,
            "items_on_sale": 8,
            "items_regular_price": 4
        }
    """
    total_regular = 0.0
    total_sale = 0.0
    items_on_sale = 0
    items_regular = 0

    for item in cart_items:
        quantity = item.get("quantity", 1)
        regular_price = item.get("regular_price")
        sale_price = item.get("price")

        if regular_price is not None:
            total_regular += regular_price * quantity

            if sale_price is not None and sale_price < regular_price:
                # On sale
                total_sale += sale_price * quantity
                items_on_sale += 1
            else:
                # Regular price
                total_sale += regular_price * quantity
                items_regular += 1
        elif sale_price is not None:
            # Only have sale price
            total_sale += sale_price * quantity
            total_regular += sale_price * quantity
            items_regular += 1

    total_savings = total_regular - total_sale
    savings_percent = (total_savings / total_regular * 100) if total_regular > 0 else 0.0

    return {
        "total_regular_price": round(total_regular, 2),
        "total_sale_price": round(total_sale, 2),
        "total_savings": round(total_savings, 2),
        "savings_percent": round(savings_percent, 1),
        "items_on_sale": items_on_sale,
        "items_regular_price": items_regular,
        "total_items": len(cart_items),
    }


def score_deal_quality(
    product: dict[str, Any],
    price_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Score how good a deal is based on multiple factors.

    Args:
        product: Product dict with pricing info
        price_stats: Optional historical price statistics

    Returns:
        {
            "quality_score": 0-100,
            "quality_label": "excellent" | "good" | "fair" | "poor",
            "urgency": "high" | "medium" | "low",
            "factors": {...}
        }
    """
    pricing = product.get("pricing", {})
    savings_percent = pricing.get("savings_percent", 0)

    score = 0
    factors: dict[str, Any] = {}

    # Factor 1: Savings percentage (0-40 points)
    if savings_percent >= 50:
        score += 40
        factors["savings"] = "exceptional"
    elif savings_percent >= 30:
        score += 30
        factors["savings"] = "excellent"
    elif savings_percent >= 20:
        score += 20
        factors["savings"] = "good"
    elif savings_percent >= 10:
        score += 10
        factors["savings"] = "fair"
    else:
        factors["savings"] = "minimal"

    # Factor 2: Historical context (0-30 points)
    if price_stats and price_stats.get("has_data"):
        rec = price_stats.get("recommendation")
        if rec == "excellent_deal":
            score += 30
            factors["historical"] = "best_price"
        elif rec == "good_deal":
            score += 20
            factors["historical"] = "below_average"
        elif rec == "fair_price":
            score += 10
            factors["historical"] = "average"
        else:
            factors["historical"] = "above_average"
    else:
        score += 15  # Default moderate score if no history
        factors["historical"] = "no_data"

    # Factor 3: User relevance (0-30 points)
    relevance_score = 0
    if product.get("is_favorite"):
        relevance_score += 15
        factors["is_favorite"] = True
    if product.get("is_in_pantry"):
        relevance_score += 10
        pantry_level = product.get("pantry_level", 100)
        if pantry_level <= 25:
            relevance_score += 5
            factors["pantry_status"] = "low"
        else:
            factors["pantry_status"] = "normal"
    if product.get("recently_purchased"):
        relevance_score += 5
        factors["recently_purchased"] = True

    score += relevance_score

    # Quality label
    if score >= 80:
        quality_label = "excellent"
        urgency = "high"
    elif score >= 60:
        quality_label = "good"
        urgency = "medium"
    elif score >= 40:
        quality_label = "fair"
        urgency = "low"
    else:
        quality_label = "poor"
        urgency = "low"

    return {
        "quality_score": score,
        "quality_label": quality_label,
        "urgency": urgency,
        "factors": factors,
    }
