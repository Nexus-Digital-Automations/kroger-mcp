"""Table-driven tests for the chatbot's tool-call risk classification."""

from __future__ import annotations

import pytest

from kroger_mcp.web import risk_policy

READ_ONLY_CASES = [
    ("cart", "view", {}),
    ("cart", "view_history", {}),
    ("cart", "get_context", {}),
    ("pantry", "get", {}),
    ("pantry", "get_low_inventory", {}),
    ("pantry", "get_attention", {}),
    ("pantry", "list_gaps", {}),
    ("favorites", "get_lists", {}),
    ("favorites", "get_items", {}),
    ("favorites", "get_low_stock", {}),
    ("favorites", "suggest", {}),
    ("favorites", "check_snacks", {}),
    ("meal_plan", "list", {}),
    ("meal_plan", "get", {}),
    ("meal_plan", "get_week_view", {}),
    ("meal_plan", "get_summary", {}),
    ("meal_plan", "preview_shopping", {}),
    ("privacy", "get_consent", {}),
    ("notion", "get_status", {}),
    ("notion", "view_recipe", {}),
    ("reports", "get_analytics", {}),
    ("reports", "export_data", {}),
    ("info", "list_chains", {}),
    ("info", "get_servings", {}),
    ("info", "get_preferences", {}),
    # cart-equivalent actions default to a harmless preview
    ("cart", "add", {"preview_only": True}),
    ("cart", "add", {}),
    ("favorites", "order", {"confirm": False}),
    ("favorites", "order", {}),
    ("meal_plan", "add_to_cart", {}),
    ("shopping_list", "add_to_cart", {}),
    ("recipes", "add_to_cart", {"confirm": False}),
]

WRITE_CASES = [
    ("pantry", "add", {}),
    ("pantry", "update_item", {}),
    ("pantry", "restock", {}),
    ("pantry", "remove", {}),
    ("pantry", "resolve_gap", {}),
    ("favorites", "create_list", {}),
    ("favorites", "rename_list", {}),
    ("favorites", "delete_list", {}),
    ("favorites", "add_item", {}),
    ("favorites", "remove_item", {}),
    ("favorites", "update_schedule", {}),
    ("favorites", "set_stock_level", {}),
    ("favorites", "update_quantity", {}),
    ("meal_plan", "create", {}),
    ("meal_plan", "update", {}),
    ("meal_plan", "delete", {}),
    ("meal_plan", "copy", {}),
    ("meal_plan", "assign_meal", {}),
    ("meal_plan", "remove_meal", {}),
    ("meal_plan", "swap", {}),
    ("meal_plan", "mark_cooked", {}),
    ("privacy", "set_consent", {}),
    ("privacy", "withdraw", {}),
    ("notion", "setup", {}),
    ("notion", "sync_all", {}),
    ("notion", "pull_changes", {}),
    ("notion", "update_tags", {}),
    ("notion", "bulk_tag", {}),
    ("info", "set_servings", {}),
]

HARD_BLOCKED_CASES = [
    ("cart", "remove", {}),
    ("cart", "clear", {}),
    ("cart", "mark_placed", {}),
    ("cart", "add", {"preview_only": False}),
    ("favorites", "order", {"confirm": True}),
    ("meal_plan", "add_to_cart", {"confirm": True}),
    ("shopping_list", "add_to_cart", {"confirm": True}),
    ("recipes", "add_to_cart", {"confirm": True}),
    ("privacy", "delete_my_data", {}),
]


@pytest.mark.parametrize("tool,action,args", READ_ONLY_CASES)
def test_read_only_classification(tool, action, args):
    assert risk_policy.classify(tool, action, args) == "read_only"


@pytest.mark.parametrize("tool,action,args", WRITE_CASES)
def test_write_classification(tool, action, args):
    assert risk_policy.classify(tool, action, args) == "write"


@pytest.mark.parametrize("tool,action,args", HARD_BLOCKED_CASES)
def test_hard_blocked_classification(tool, action, args):
    assert risk_policy.classify(tool, action, args) == "hard_blocked"


def test_unknown_action_defaults_to_write_not_silent_auto_run():
    assert risk_policy.classify("cart", "some_new_action", {}) == "write"
