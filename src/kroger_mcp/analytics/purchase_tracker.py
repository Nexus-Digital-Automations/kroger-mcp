"""
Purchase event tracking - records cart additions and completed orders.
"""

import logging
import sqlite3
from datetime import datetime
from typing import Any

from .database import ensure_initialized, get_db_connection, insert_returning_id

logger = logging.getLogger(__name__)


def _ensure_product_exists_conn(
    conn: sqlite3.Connection,
    product_id: str,
    product_details: dict[str, Any] | None = None,
) -> None:
    """Ensure a product record exists using an existing connection (no commit)."""
    cursor = conn.execute(
        "SELECT id FROM products WHERE product_id = ?",
        (product_id,),
    )
    if cursor.fetchone() is None:
        now = datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO products
            (product_id, upc, description, brand, first_purchased_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                product_details.get("upc") if product_details else None,
                product_details.get("description") if product_details else None,
                product_details.get("brand") if product_details else None,
                now,
                now,
            ),
        )


def ensure_product_exists(product_id: str, product_details: dict[str, Any] | None = None) -> None:
    """
    Ensure a product record exists in the database.

    Args:
        product_id: The product identifier
        product_details: Optional dict with upc, description, brand
    """
    ensure_initialized()
    conn = get_db_connection()
    try:
        # Check if product exists
        cursor = conn.execute("SELECT id FROM products WHERE product_id = ?", (product_id,))
        if cursor.fetchone() is None:
            # Insert new product
            now = datetime.now().isoformat()
            conn.execute(
                """
                INSERT INTO products
                (product_id, upc, description, brand, first_purchased_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    product_id,
                    product_details.get("upc") if product_details else None,
                    product_details.get("description") if product_details else None,
                    product_details.get("brand") if product_details else None,
                    now,
                    now,
                ),
            )
            conn.commit()
    finally:
        conn.close()


def record_cart_add(
    product_id: str,
    quantity: int,
    modality: str,
    product_details: dict[str, Any] | None = None,
    price: float | None = None,
) -> int:
    """
    Record a cart addition event.

    Args:
        product_id: The product identifier
        quantity: Quantity added
        modality: 'PICKUP' or 'DELIVERY'
        product_details: Optional product metadata
        price: Optional price at time of addition

    Returns:
        The ID of the created purchase event
    """
    ensure_initialized()

    # Ensure product exists
    ensure_product_exists(product_id, product_details)

    conn = get_db_connection()
    try:
        now = datetime.now()
        event_id = insert_returning_id(
            conn,
            """
            INSERT INTO purchase_events
            (product_id, quantity, event_type, modality, price, event_date, event_timestamp)
            VALUES (?, ?, 'cart_add', ?, ?, ?, ?)
        """,
            (product_id, quantity, modality, price, now.strftime("%Y-%m-%d"), now.isoformat()),
        )
        conn.commit()
        if event_id is None:
            raise RuntimeError("Failed to record cart-add event")
        return event_id
    finally:
        conn.close()


def record_order(cart_items: list[dict[str, Any]], order_notes: str | None = None) -> int:
    """
    Record a completed order and link cart items to it.

    Also restocks pantry items for any products being tracked.

    Args:
        cart_items: List of cart items, each with product_id, quantity, modality
        order_notes: Optional notes about the order

    Returns:
        The ID of the created order
    """
    ensure_initialized()

    conn = get_db_connection()
    try:
        now = datetime.now()

        # Calculate totals
        item_count = len(cart_items)
        total_quantity = sum(item.get("quantity", 1) for item in cart_items)

        # Create order record
        order_id = insert_returning_id(
            conn,
            """
            INSERT INTO orders (placed_at, item_count, total_quantity, notes)
            VALUES (?, ?, ?, ?)
        """,
            (now.isoformat(), item_count, total_quantity, order_notes),
        )
        if order_id is None:
            raise RuntimeError("Failed to create order record")

        # Record purchase events for each item
        for item in cart_items:
            product_id = item.get("product_id")
            if product_id is None:
                continue
            product_id = str(product_id)
            quantity = item.get("quantity", 1)
            modality = item.get("modality", "PICKUP")

            # Ensure product exists using the same connection to avoid DB lock
            _ensure_product_exists_conn(conn, product_id, item)

            # Insert purchase event
            conn.execute(
                """
                INSERT INTO purchase_events
                (product_id, quantity, event_type, modality, event_date,
                 event_timestamp, order_id)
                VALUES (?, ?, 'order_placed', ?, ?, ?, ?)
            """,
                (
                    product_id,
                    quantity,
                    modality,
                    now.strftime("%Y-%m-%d"),
                    now.isoformat(),
                    order_id,
                ),
            )

        conn.commit()

        # Auto-restock pantry items (after commit to ensure order is recorded)
        _restock_pantry_items(cart_items)

        return order_id
    finally:
        conn.close()


def _restock_pantry_items(cart_items: list[dict[str, Any]]) -> None:
    """
    Restock pantry items for products in the order.

    Adds items to pantry tracking if not already there, then sets level to 100%.

    Args:
        cart_items: List of cart items from the order
    """
    try:
        from .pantry import add_to_pantry, get_pantry_item, restock_item

        for item in cart_items:
            product_id = item.get("product_id")
            if not product_id:
                continue
            description = item.get("description")
            pantry_item = get_pantry_item(product_id)
            if pantry_item:
                restock_item(product_id, level=100, description=description)
            else:
                # Not yet tracked — add to pantry at 100%
                add_to_pantry(product_id=product_id, description=description, level=100)
    except Exception as e:
        logger.warning("Could not restock pantry items: %s", e, exc_info=True)


def get_purchase_events(
    product_id: str, event_type: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """
    Get purchase events for a product.

    Args:
        product_id: The product identifier
        event_type: Optional filter by event type ('cart_add' or 'order_placed')
        limit: Maximum number of events to return

    Returns:
        List of purchase event dictionaries
    """
    ensure_initialized()

    conn = get_db_connection()
    try:
        query = """
            SELECT * FROM purchase_events
            WHERE product_id = ?
        """
        params: list[Any] = [product_id]

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        query += " ORDER BY event_timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_order_history(limit: int = 50) -> list[dict[str, Any]]:
    """
    Get order history.

    Args:
        limit: Maximum number of orders to return

    Returns:
        List of order dictionaries with their items
    """
    ensure_initialized()

    conn = get_db_connection()
    try:
        # Get orders
        cursor = conn.execute(
            """
            SELECT * FROM orders
            ORDER BY placed_at DESC
            LIMIT ?
        """,
            (limit,),
        )
        orders = [dict(row) for row in cursor.fetchall()]

        # Get items for each order
        for order in orders:
            items_cursor = conn.execute(
                """
                SELECT pe.*, p.description, p.brand
                FROM purchase_events pe
                LEFT JOIN products p ON pe.product_id = p.product_id
                WHERE pe.order_id = ?
            """,
                (order["id"],),
            )
            order["items"] = [dict(row) for row in items_cursor.fetchall()]

        return orders
    finally:
        conn.close()


def get_all_products() -> list[dict[str, Any]]:
    """
    Get all tracked products.

    Returns:
        List of product dictionaries
    """
    ensure_initialized()

    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT * FROM products ORDER BY description")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
