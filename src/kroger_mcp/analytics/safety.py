"""
Product safety checking and management.

This module provides functions for:
- Checking products against the bad ingredients list
- Managing safe-listed and blocked products
- Managing user preferences for ingredient filtering
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from kroger_mcp.auth.dependencies import mcp_user_id

from .database import ensure_initialized, get_db_cursor
from .ingredients import (
    SafetyResult,
    check_product_safety,
)


def _resolve_user_id(user_id: str | None) -> str:
    """Resolve user_id for user-scoped queries.

    HTTP route handlers always pass user_id from the session. MCP/script
    callers may pass None; we fall back to `mcp_user_id()` which honors
    KROGER_MCP_USER_ID per Claude Desktop profile, then
    KROGER_MCP_DEFAULT_USER_ID. This means MCP profiles bound to different
    users see only their own data — no per-tool-dispatcher threading needed.
    """
    return user_id if user_id is not None else mcp_user_id()


def _ensure_default_safety_settings_for_user(user_id: str) -> None:
    """Lazily create default safety_settings rows for a user that has none.

    Mirrors `_ensure_default_list_for_user` in favorites.py — new users
    that have never touched safety yet get the same defaults the migration
    backfilled for the seed owner (filtering_enabled=1, block_mode='soft').
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM safety_settings WHERE user_id = ?",
            (user_id,),
        )
        if cursor.fetchone()["cnt"] > 0:
            return
        now = datetime.now().isoformat()
        cursor.execute(
            """
            INSERT INTO safety_settings (user_id, key, value, updated_at)
            VALUES (?, 'filtering_enabled', '1', ?)
            """,
            (user_id, now),
        )
        cursor.execute(
            """
            INSERT INTO safety_settings (user_id, key, value, updated_at)
            VALUES (?, 'block_mode', 'soft', ?)
            """,
            (user_id, now),
        )


class SafetyStatus(str, Enum):
    """Overall safety status for a product."""

    SAFE = "safe"  # On safe list (explicitly approved)
    EXCELLENT = "excellent"  # Score 90-100: premium quality markers
    GOOD = "good"  # Score 75-89: clean product with bonuses
    ACCEPTABLE = "acceptable"  # Score 60-74: no concerns detected
    POOR = "poor"  # Score 45-59: watch-level concerns
    AVOID = "avoid"  # Score 0-44: critical/warning ingredients
    BLOCKED = "blocked"  # On blocked list (explicitly blocked)


class BlockMode(str, Enum):
    """How to handle flagged products."""

    SOFT = "soft"  # Warn but allow with confirmation
    HARD = "hard"  # Hide from search, block cart additions
    WARN_ONLY = "warn_only"  # Just show warnings, no blocking


@dataclass
class ProductSafetyStatus:
    """Complete safety status for a product."""

    product_id: str
    status: SafetyStatus
    is_safe_listed: bool
    is_blocked: bool
    blocked_reason: str | None
    safety_result: SafetyResult | None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "product_id": self.product_id,
            "safety_status": self.status.value,
            "is_safe_listed": self.is_safe_listed,
            "is_blocked": self.is_blocked,
        }
        if self.is_blocked and self.blocked_reason:
            result["blocked_reason"] = self.blocked_reason
        if self.safety_result:
            sr = self.safety_result.to_dict()
            result["safety_score"] = sr["score"]
            result["safety_grade"] = sr["grade"]
            result["positive_attributes"] = sr["positive_attributes"]
            result["flagged_ingredients"] = sr["flagged_ingredients"]
        else:
            result["safety_score"] = None
            result["safety_grade"] = None
            result["positive_attributes"] = []
            result["flagged_ingredients"] = []
        return result


# ============== Settings Management ==============


def get_safety_settings(user_id: str | None = None) -> dict[str, Any]:
    """Get current safety filter settings for a user.

    First-read for a user with no rows seeds per-user defaults so subsequent
    callers see the same baseline the migration backfilled for the seed owner.
    """
    ensure_initialized()
    resolved = _resolve_user_id(user_id)
    _ensure_default_safety_settings_for_user(resolved)

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT key, value FROM safety_settings WHERE user_id = ?", (resolved,)
        )
        rows = cursor.fetchall()

    settings = {
        "filtering_enabled": True,
        "block_mode": BlockMode.SOFT.value,
    }

    for row in rows:
        key = row["key"]
        value = row["value"]
        if key == "filtering_enabled":
            settings["filtering_enabled"] = value == "1"
        elif key == "block_mode":
            settings["block_mode"] = value

    return settings


def update_safety_settings(
    filtering_enabled: bool | None = None,
    block_mode: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Update safety filter settings for a user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        now = datetime.now().isoformat()

        if filtering_enabled is not None:
            value = "1" if filtering_enabled else "0"
            cursor.execute(
                """
                INSERT INTO safety_settings (user_id, key, value, updated_at)
                VALUES (?, 'filtering_enabled', ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value = ?, updated_at = ?
                """,
                (resolved, value, now, value, now),
            )

        if block_mode is not None:
            if block_mode not in [m.value for m in BlockMode]:
                raise ValueError(f"Invalid block_mode: {block_mode}")
            cursor.execute(
                """
                INSERT INTO safety_settings (user_id, key, value, updated_at)
                VALUES (?, 'block_mode', ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value = ?, updated_at = ?
                """,
                (resolved, block_mode, now, block_mode, now),
            )

    return get_safety_settings(user_id=resolved)


# ============== Safe Products Management ==============


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
                description = COALESCE(?, description),
                brand = COALESCE(?, brand),
                added_reason = COALESCE(?, added_reason),
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


# ============== Blocked Products Management ==============


def is_product_blocked(
    product_id: str, user_id: str | None = None
) -> tuple[bool, str | None]:
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


def get_all_blocked_product_ids(user_id: str | None = None) -> set[str]:
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
    user_id: str | None = None,
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
                description = COALESCE(?, description),
                blocked_reason = COALESCE(?, blocked_reason),
                blocked_at = CURRENT_TIMESTAMP
            """,
            (
                resolved,
                product_id,
                description,
                reason,
                1 if auto_blocked else 0,
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


def remove_from_blocked_list(
    product_id: str, user_id: str | None = None
) -> dict[str, Any]:
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


def get_blocked_products(user_id: str | None = None) -> list[dict[str, Any]]:
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


# ============== Ingredient Preferences ==============


def get_disabled_ingredients(user_id: str | None = None) -> set[str]:
    """Get set of ingredient keys that this user has disabled."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT ingredient_key FROM ingredient_preferences WHERE user_id = ? AND enabled = 0",
            (resolved,),
        )
        return {row["ingredient_key"] for row in cursor.fetchall()}


def toggle_ingredient(
    ingredient_key: str, enabled: bool, user_id: str | None = None
) -> dict[str, Any]:
    """Enable or disable checking for a specific ingredient for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)
    flag = 1 if enabled else 0

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ingredient_preferences (user_id, ingredient_key, enabled, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, ingredient_key) DO UPDATE SET
                enabled = ?,
                updated_at = CURRENT_TIMESTAMP
            """,
            (resolved, ingredient_key, flag, flag),
        )

    return {
        "success": True,
        "ingredient_key": ingredient_key,
        "enabled": enabled,
    }


def get_ingredient_preferences(user_id: str | None = None) -> list[dict[str, Any]]:
    """Get all ingredient preferences for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT ingredient_key, enabled, severity, updated_at
            FROM ingredient_preferences
            WHERE user_id = ?
            ORDER BY ingredient_key
            """,
            (resolved,),
        )
        return [dict(row) for row in cursor.fetchall()]


def reset_ingredient_preferences(user_id: str | None = None) -> dict[str, Any]:
    """Reset all ingredient preferences to defaults (all enabled) for this user."""
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM ingredient_preferences WHERE user_id = ?", (resolved,))
        deleted = cursor.rowcount

    return {"success": True, "message": f"Reset {deleted} ingredient preferences to defaults"}


# ============== Product Safety Checking ==============


def get_product_safety_status(
    product_id: str,
    description: str,
    brand: str | None = None,
    categories: list[str] | None = None,
    user_id: str | None = None,
) -> ProductSafetyStatus:
    """
    Get the complete safety status for a product, scoped to a user.

    This checks:
    1. If product is on the user's safe list (bypasses all checks)
    2. If product is on the user's blocked list
    3. If product matches any bad ingredients (respecting user's disabled set)

    Args:
        product_id: Kroger product ID
        description: Product description to scan
        brand: Product brand (not scanned)
        categories: Product categories
        user_id: User scope; None resolves via `_resolve_user_id`.

    Returns:
        ProductSafetyStatus with complete safety information
    """
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    if is_product_safe_listed(product_id, user_id=resolved):
        return ProductSafetyStatus(
            product_id=product_id,
            status=SafetyStatus.SAFE,
            is_safe_listed=True,
            is_blocked=False,
            blocked_reason=None,
            safety_result=None,
        )

    is_blocked, blocked_reason = is_product_blocked(product_id, user_id=resolved)
    if is_blocked:
        return ProductSafetyStatus(
            product_id=product_id,
            status=SafetyStatus.BLOCKED,
            is_safe_listed=False,
            is_blocked=True,
            blocked_reason=blocked_reason,
            safety_result=None,
        )

    disabled = get_disabled_ingredients(user_id=resolved)

    # Check ingredients
    safety_result = check_product_safety(
        description=description,
        brand=brand,
        categories=categories,
        disabled_ingredients=disabled,
    )

    # Determine status based on safety score
    score = safety_result.score
    if score >= 90:
        status = SafetyStatus.EXCELLENT
    elif score >= 75:
        status = SafetyStatus.GOOD
    elif score >= 60:
        status = SafetyStatus.ACCEPTABLE
    elif score >= 45:
        status = SafetyStatus.POOR
    else:
        status = SafetyStatus.AVOID

    return ProductSafetyStatus(
        product_id=product_id,
        status=status,
        is_safe_listed=False,
        is_blocked=False,
        blocked_reason=None,
        safety_result=safety_result,
    )


def check_products_safety_batch(
    products: list[dict[str, Any]],
    user_id: str | None = None,
) -> list[ProductSafetyStatus]:
    """
    Check safety status for multiple products efficiently, scoped to a user.

    Args:
        products: List of product dicts with 'product_id', 'description', 'brand'
        user_id: User scope; None resolves via `_resolve_user_id`.

    Returns:
        List of ProductSafetyStatus objects
    """
    ensure_initialized()
    resolved = _resolve_user_id(user_id)

    # Pre-load safe and blocked lists for O(1) lookups per user.
    safe_ids = get_all_safe_product_ids(user_id=resolved)
    blocked_ids = get_all_blocked_product_ids(user_id=resolved)
    disabled = get_disabled_ingredients(user_id=resolved)

    blocked_reasons: dict[str, str] = {}
    if blocked_ids:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT product_id, blocked_reason FROM blocked_products WHERE user_id = ?",
                (resolved,),
            )
            for row in cursor.fetchall():
                blocked_reasons[row["product_id"]] = row["blocked_reason"]

    results = []
    for product in products:
        product_id = product.get("product_id", "")
        description = product.get("description", "")
        brand = product.get("brand")

        # Check safe list
        if product_id in safe_ids:
            results.append(
                ProductSafetyStatus(
                    product_id=product_id,
                    status=SafetyStatus.SAFE,
                    is_safe_listed=True,
                    is_blocked=False,
                    blocked_reason=None,
                    safety_result=None,
                )
            )
            continue

        # Check blocked list
        if product_id in blocked_ids:
            results.append(
                ProductSafetyStatus(
                    product_id=product_id,
                    status=SafetyStatus.BLOCKED,
                    is_safe_listed=False,
                    is_blocked=True,
                    blocked_reason=blocked_reasons.get(product_id),
                    safety_result=None,
                )
            )
            continue

        # Check ingredients
        safety_result = check_product_safety(
            description=description,
            brand=brand,
            disabled_ingredients=disabled,
        )

        # Determine status based on safety score
        score = safety_result.score
        if score >= 90:
            status = SafetyStatus.EXCELLENT
        elif score >= 75:
            status = SafetyStatus.GOOD
        elif score >= 60:
            status = SafetyStatus.ACCEPTABLE
        elif score >= 45:
            status = SafetyStatus.POOR
        else:
            status = SafetyStatus.AVOID

        results.append(
            ProductSafetyStatus(
                product_id=product_id,
                status=status,
                is_safe_listed=False,
                is_blocked=False,
                blocked_reason=None,
                safety_result=safety_result,
            )
        )

    return results


def is_filtering_enabled(user_id: str | None = None) -> bool:
    """Check if ingredient filtering is enabled for this user."""
    settings = get_safety_settings(user_id=user_id)
    return settings.get("filtering_enabled", True)


def get_block_mode(user_id: str | None = None) -> BlockMode:
    """Get the current block mode for this user."""
    settings = get_safety_settings(user_id=user_id)
    mode_str = settings.get("block_mode", "soft")
    return BlockMode(mode_str)


# ==================== ASYNC WRAPPERS ====================
# Use these from async tool handlers to avoid blocking the event loop.


async def check_products_safety_batch_async(
    products: list[dict[str, Any]],
    user_id: str | None = None,
) -> list[ProductSafetyStatus]:
    """Async wrapper for check_products_safety_batch() — runs in thread pool."""
    return await asyncio.to_thread(check_products_safety_batch, products, user_id=user_id)


async def get_product_safety_status_async(
    product_id: str,
    description: str,
    brand: str | None = None,
    categories: list[str] | None = None,
    user_id: str | None = None,
) -> ProductSafetyStatus:
    """Async wrapper for get_product_safety_status() — runs in thread pool."""
    return await asyncio.to_thread(
        get_product_safety_status,
        product_id,
        description,
        brand,
        categories,
        user_id=user_id,
    )
