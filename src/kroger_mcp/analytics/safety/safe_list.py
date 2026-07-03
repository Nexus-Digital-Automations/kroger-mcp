"""Per-user safe list (explicitly approved products that bypass checks)."""

from typing import Any

from ..database import ensure_initialized, get_db_cursor
from ._common import _resolve_user_id


def is_product_safe_listed(product_id: str, user_id: str | None = None) -> bool:
    """Check if a product is on the safe list for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM safe_products WHERE user_id = ? AND product_id = ?",
            (resolved, product_id),
        )
        return cursor.fetchone() is not None


def get_all_safe_product_ids(user_id: str | None = None) -> set[str]:
    """Get all safe-listed product IDs for fast lookup."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute("SELECT product_id FROM safe_products WHERE user_id = ?", (resolved,))
        return {row["product_id"] for row in cursor.fetchall()}


def add_to_safe_list(
    product_id: str,
    description: str | None = None,
    brand: str | None = None,
    reason: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Add a product to the safe list for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO safe_products (user_id, product_id, description, brand, added_reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, product_id) DO UPDATE SET
                description = COALESCE(?, safe_products.description),
                brand = COALESCE(?, safe_products.brand),
                added_reason = COALESCE(?, safe_products.added_reason),
                added_at = CURRENT_TIMESTAMP
            """,
            (resolved, product_id, description, brand, reason, description, brand, reason),
        )

        # Approving a product overrides any existing block for the same user.
        cursor.execute(
            "DELETE FROM blocked_products WHERE user_id = ? AND product_id = ?",
            (resolved, product_id),
        )

    return {
        "success": True,
        "product_id": product_id,
        "message": f"Product {product_id} added to safe list",
    }


def remove_from_safe_list(product_id: str, user_id: str | None = None) -> dict[str, Any]:
    """Remove a product from the safe list for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            "DELETE FROM safe_products WHERE user_id = ? AND product_id = ?",
            (resolved, product_id),
        )
        deleted = cursor.rowcount

    if deleted:
        return {
            "success": True,
            "product_id": product_id,
            "message": f"Product {product_id} removed from safe list",
        }
    return {
        "success": False,
        "product_id": product_id,
        "message": f"Product {product_id} was not on safe list",
    }


def get_safe_products(user_id: str | None = None) -> list[dict[str, Any]]:
    """Get all products on the safe list for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT product_id, description, brand, added_at, added_reason
            FROM safe_products
            WHERE user_id = ?
            ORDER BY added_at DESC
            """,
            (resolved,),
        )
        return [dict(row) for row in cursor.fetchall()]
