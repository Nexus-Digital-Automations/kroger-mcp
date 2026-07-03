"""
Product safety checking and management.

This package provides functions for:
- Checking products against the bad ingredients list
- Managing safe-listed and blocked products
- Managing user preferences for ingredient filtering

The implementation is split across focused submodules (models, settings,
safe_list, blocked_list, ingredient_prefs, checks, _cache); this package
re-exports the full public API so ``from kroger_mcp.analytics.safety import X``
keeps working unchanged.
"""

from .blocked_list import (
    add_to_blocked_list,
    get_all_blocked_product_ids,
    get_blocked_products,
    is_product_blocked,
    remove_from_blocked_list,
)
from .checks import (
    check_products_safety_batch,
    check_products_safety_batch_async,
    get_product_safety_status,
    get_product_safety_status_async,
)
from .ingredient_prefs import (
    get_disabled_ingredients,
    get_ingredient_preferences,
    reset_ingredient_preferences,
    toggle_ingredient,
)
from .models import BlockMode, ProductSafetyStatus, SafetyStatus
from .safe_list import (
    add_to_safe_list,
    get_all_safe_product_ids,
    get_safe_products,
    is_product_safe_listed,
    remove_from_safe_list,
)
from .settings import (
    get_block_mode,
    get_safety_settings,
    is_filtering_enabled,
    update_safety_settings,
)

__all__ = [
    "BlockMode",
    "ProductSafetyStatus",
    "SafetyStatus",
    "add_to_blocked_list",
    "add_to_safe_list",
    "check_products_safety_batch",
    "check_products_safety_batch_async",
    "get_all_blocked_product_ids",
    "get_all_safe_product_ids",
    "get_block_mode",
    "get_blocked_products",
    "get_disabled_ingredients",
    "get_ingredient_preferences",
    "get_product_safety_status",
    "get_product_safety_status_async",
    "get_safe_products",
    "get_safety_settings",
    "is_filtering_enabled",
    "is_product_blocked",
    "is_product_safe_listed",
    "remove_from_blocked_list",
    "remove_from_safe_list",
    "reset_ingredient_preferences",
    "toggle_ingredient",
    "update_safety_settings",
]
