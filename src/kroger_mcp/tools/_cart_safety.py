"""
Shared cart-item safety-check helper.

Extracted from cart_tools.py's `add` action so every cart-write call site
(cart_tools.add, recipe_tools.add_to_cart, shopping_list_tools.add_to_cart,
favorites_tools.order) enforces the same ingredient-safety gate before
hitting the real Kroger API.
"""

from typing import Any

from ..analytics.ingredients import check_product_safety
from ..analytics.safety import (
    BlockMode,
    get_all_blocked_product_ids,
    get_all_safe_product_ids,
    get_block_mode,
    get_disabled_ingredients,
    is_filtering_enabled,
)


def check_cart_items_safety(
    items: list[dict[str, Any]],
    *,
    user_id: str,
    confirm_unsafe: bool = False,
) -> dict[str, Any] | None:
    """Check a batch of cart items against the safety filter.

    `items` are dicts with at least `product_id`; `description` is used for
    ingredient matching when present. Returns None when the batch is clear
    to add, else a response dict the caller should return as-is:
    `requires_confirmation` (soft mode — call again with confirm_unsafe=True)
    or a hard block (`requires_confirmation: False` — hard mode never
    accepts confirm_unsafe; the caller must remove the item or switch modes).

    warn_only mode never blocks (always returns None once filtering allows
    the batch through).
    """
    if not is_filtering_enabled(user_id=user_id):
        return None

    block_mode = get_block_mode(user_id=user_id)

    # Hard mode can't be bypassed via confirm_unsafe -- only soft/warn_only
    # short-circuit once the caller has confirmed.
    if confirm_unsafe and block_mode != BlockMode.HARD:
        return None

    safe_ids = get_all_safe_product_ids(user_id=user_id)
    blocked_ids_set = get_all_blocked_product_ids(user_id=user_id)
    disabled_ingredients = get_disabled_ingredients(user_id=user_id)

    safety_warnings = []
    blocked_items = []

    for item in items:
        pid = item["product_id"]
        description = item.get("description") or ""

        if pid in safe_ids:
            continue
        if pid in blocked_ids_set:
            blocked_items.append(
                {
                    "product_id": pid,
                    "description": description,
                    "reason": "Product is on your blocked list",
                }
            )
            continue
        if description:
            safety_result = check_product_safety(
                description=description,
                disabled_ingredients=disabled_ingredients,
                user_id=user_id,
            )
            if safety_result.has_concerns:
                safety_warnings.append(
                    {
                        "product_id": pid,
                        "description": description,
                        "severity": (
                            safety_result.highest_severity.value
                            if safety_result.highest_severity
                            else ""
                        ),
                        "flagged_ingredients": [
                            {
                                "ingredient": match.ingredient_name,
                                "severity": match.severity.value,
                                "reason": match.reason,
                                "matched_text": match.matched_text,
                            }
                            for match in safety_result.matches
                        ],
                    }
                )

    if not (blocked_items or safety_warnings):
        return None

    if block_mode == BlockMode.WARN_ONLY:
        return None

    if block_mode == BlockMode.HARD:
        return {
            "success": False,
            "requires_confirmation": False,
            "message": (
                "Some products are blocked by your safety settings "
                "(hard block mode) and cannot be added to cart."
            ),
            "blocked_items": blocked_items,
            "safety_warnings": safety_warnings,
            "total_flagged": len(blocked_items) + len(safety_warnings),
            "items_requested": len(items),
            "next_step": (
                "Remove flagged items from your request, switch to soft "
                "block mode in Settings, or use "
                "safety(action='approve_product') to safe-list products you trust."
            ),
        }

    return {
        "success": False,
        "requires_confirmation": True,
        "message": (
            "Some products have safety concerns. "
            "Set confirm_unsafe=True to add anyway."
        ),
        "blocked_items": blocked_items,
        "safety_warnings": safety_warnings,
        "total_flagged": len(blocked_items) + len(safety_warnings),
        "items_requested": len(items),
        "next_step": (
            "Review the flagged ingredients and either: "
            "(1) call again with confirm_unsafe=True to add anyway, "
            "(2) remove flagged items from your request, or "
            "(3) use safety(action='approve_product') to safe-list products you trust"
        ),
    }
