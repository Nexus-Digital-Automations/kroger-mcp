"""
Analytics package for purchase tracking, statistics, and predictions.

This package provides:
- SQLite-based purchase event tracking
- Statistical analysis of consumption patterns
- Repurchase prediction using exponential weighted moving averages
- Automatic and manual item categorization (routine/regular/treat)
- Seasonal/holiday pattern detection
"""

from .categories import (
    detect_category,
    get_items_by_category,
    get_product_category,
    set_product_category,
)
from .config import (
    PredictionConfig,
    get_config_summary,
    load_config,
    reset_config,
    save_config,
    update_config,
)
from .database import (
    ensure_initialized,
    get_db_connection,
    initialize_database,
)
from .migration import (
    migrate_json_to_sqlite,
    needs_migration,
)
from .pantry import (
    add_to_pantry,
    calculate_depletion_rate,
    consume_from_pantry,
    get_low_inventory_items,
    get_pantry_item,
    get_pantry_status,
    remove_from_pantry,
    restock_item,
    update_pantry_level,
)
from .predictions import (
    get_predictions_for_period,
    get_shopping_suggestions,
    get_urgency_label,
    predict_repurchase_date,
)
from .purchase_tracker import (
    ensure_product_exists,
    record_cart_add,
    record_order,
)
from .recipe_integration import (
    check_recipe_pantry,
    generate_shopping_list,
    get_recipes_for_pantry,
)
from .reporting import (
    export_all_data,
    generate_pantry_report,
    generate_patterns_report,
    generate_prediction_accuracy_report,
    generate_spending_report,
)
from .seasonal import (
    calculate_seasonality_score,
    detect_holiday_association,
    get_holiday_date,
    get_upcoming_holidays,
    get_upcoming_seasonal_items,
)
from .statistics import (
    calculate_consumption_rate,
    get_product_statistics,
    update_all_product_stats,
    update_product_stats,
)

__all__ = [
    # Database
    "get_db_connection",
    "initialize_database",
    "ensure_initialized",
    # Purchase tracking
    "record_cart_add",
    "record_order",
    "ensure_product_exists",
    # Statistics
    "calculate_consumption_rate",
    "update_product_stats",
    "update_all_product_stats",
    "get_product_statistics",
    # Categories
    "detect_category",
    "set_product_category",
    "get_product_category",
    "get_items_by_category",
    # Predictions
    "predict_repurchase_date",
    "get_predictions_for_period",
    "get_urgency_label",
    "get_shopping_suggestions",
    # Seasonal
    "calculate_seasonality_score",
    "detect_holiday_association",
    "get_upcoming_seasonal_items",
    "get_holiday_date",
    "get_upcoming_holidays",
    # Migration
    "needs_migration",
    "migrate_json_to_sqlite",
    # Pantry
    "restock_item",
    "update_pantry_level",
    "add_to_pantry",
    "remove_from_pantry",
    "get_pantry_status",
    "get_low_inventory_items",
    "get_pantry_item",
    "calculate_depletion_rate",
    "consume_from_pantry",
    # Config
    "load_config",
    "save_config",
    "update_config",
    "reset_config",
    "get_config_summary",
    "PredictionConfig",
    # Recipe Integration
    "check_recipe_pantry",
    "generate_shopping_list",
    "get_recipes_for_pantry",
    # Reporting
    "generate_spending_report",
    "generate_prediction_accuracy_report",
    "generate_patterns_report",
    "generate_pantry_report",
    "export_all_data",
]
