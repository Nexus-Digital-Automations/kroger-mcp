"""Per-user blocked list (explicitly blocked products)."""

from typing import Any

from ..database import ensure_initialized, get_db_cursor
from ._common import _resolve_user_id


def is_product_blocked(product_id: str, user_id: str) -> tuple[bool, str | None]:
    """Check if a product is on the blocked list for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT blocked_reason FROM blocked_products WHERE user_id = ? AND product_id = ?",
            (resolved, product_id),
        )
        row = cursor.fetchone()
        if row:
            return True, row["blocked_reason"]
        return False, None


def get_all_blocked_product_ids(user_id: str) -> set[str]:
    """Get all blocked product IDs for fast lookup."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute("SELECT product_id FROM blocked_products WHERE user_id = ?", (resolved,))
        return {row["product_id"] for row in cursor.fetchall()}


def add_to_blocked_list(
    product_id: str,
    description: str | None = None,
    reason: str | None = None,
    auto_blocked: bool = False,
    *, user_id: str,
) -> dict[str, Any]:
    """Add a product to the blocked list for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO blocked_products
                (user_id, product_id, description, blocked_reason, auto_blocked)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, product_id) DO UPDATE SET
                description = COALESCE(?, blocked_products.description),
                blocked_reason = COALESCE(?, blocked_products.blocked_reason),
                blocked_at = CURRENT_TIMESTAMP
            """,
            (
                resolved,
                product_id,
                description,
                reason,
                bool(auto_blocked),
                description,
                reason,
            ),
        )

        # Blocking a product overrides any existing safe-list entry for the same user.
        cursor.execute(
            "DELETE FROM safe_products WHERE user_id = ? AND product_id = ?",
            (resolved, product_id),
        )

    return {
        "success": True,
        "product_id": product_id,
        "message": f"Product {product_id} added to blocked list",
    }


def remove_from_blocked_list(product_id: str, user_id: str) -> dict[str, Any]:
    """Remove a product from the blocked list for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            "DELETE FROM blocked_products WHERE user_id = ? AND product_id = ?",
            (resolved, product_id),
        )
        deleted = cursor.rowcount

    if deleted:
        return {
            "success": True,
            "product_id": product_id,
            "message": f"Product {product_id} removed from blocked list",
        }
    return {
        "success": False,
        "product_id": product_id,
        "message": f"Product {product_id} was not on blocked list",
    }


def get_blocked_products(user_id: str) -> list[dict[str, Any]]:
    """Get all products on the blocked list for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT product_id, description, blocked_at, blocked_reason, auto_blocked
            FROM blocked_products
            WHERE user_id = ?
            ORDER BY blocked_at DESC
            """,
            (resolved,),
        )
        return [dict(row) for row in cursor.fetchall()]
