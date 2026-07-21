"""
Unit tests for shopping list functionality.
"""

import pytest

from kroger_mcp.analytics.database import get_db_connection
from kroger_mcp.auth.dependencies import default_user_id
from kroger_mcp.tools.shopping_list_tools import (
    _consolidate_items,
    _generate_list_item_id,
    _load_shopping_list,
    _save_shopping_list,
)


@pytest.fixture(autouse=True)
def cleanup_shopping_list():
    """Wipe this user's shopping list row(s) before and after each test."""

    def _clear():
        conn = get_db_connection()
        try:
            conn.execute(
                "DELETE FROM user_shopping_lists WHERE user_id = ?",
                (default_user_id(),),
            )
            conn.commit()
        finally:
            conn.close()

    _clear()
    yield
    _clear()


def test_load_empty_shopping_list():
    """Spec: load returns empty items + None last_updated when nothing stored."""
    data = _load_shopping_list(user_id=default_user_id())
    assert data == {"items": [], "last_updated": None}


def test_save_and_load_shopping_list():
    """Spec: round-trip a single item through user_shopping_lists DB table."""
    data = {
        "items": [
            {
                "id": "list_item_001",
                "product_id": "12345",
                "name": "Eggs",
                "quantity": 2,
            }
        ]
    }

    _save_shopping_list(data, user_id=default_user_id())

    loaded = _load_shopping_list(user_id=default_user_id())
    assert len(loaded["items"]) == 1
    assert loaded["items"][0]["name"] == "Eggs"
    assert "last_updated" in loaded


def test_generate_list_item_id():
    """Test that generated IDs are unique."""
    id1 = _generate_list_item_id()
    id2 = _generate_list_item_id()

    assert id1 != id2
    assert id1.startswith("list_item_")
    assert id2.startswith("list_item_")


def test_consolidate_items_with_same_product():
    """Test consolidating items with the same product_id."""
    items = [
        {
            "id": "item1",
            "product_id": "12345",
            "ingredient_name": "Eggs",
            "quantity": 2,
            "sources": [{"recipe_id": "recipe1"}]
        },
        {
            "id": "item2",
            "product_id": "12345",
            "ingredient_name": "Eggs",
            "quantity": 4,
            "sources": [{"recipe_id": "recipe2"}]
        }
    ]

    consolidated = _consolidate_items(items)

    assert len(consolidated) == 1
    assert consolidated[0]["quantity"] == 6  # 2 + 4
    assert len(consolidated[0]["sources"]) == 2


def test_consolidate_items_with_different_products():
    """Test that items with different product_ids stay separate."""
    items = [
        {
            "id": "item1",
            "product_id": "12345",
            "ingredient_name": "Eggs",
            "quantity": 2
        },
        {
            "id": "item2",
            "product_id": "67890",
            "ingredient_name": "Milk",
            "quantity": 1
        }
    ]

    consolidated = _consolidate_items(items)

    assert len(consolidated) == 2


def test_consolidate_items_without_product_id():
    """Test that items without product_id stay separate."""
    items = [
        {
            "id": "item1",
            "ingredient_name": "Eggs",
            "quantity": 2
        },
        {
            "id": "item2",
            "ingredient_name": "Milk",
            "quantity": 1
        }
    ]

    consolidated = _consolidate_items(items)

    # Items without product_id should not be consolidated
    assert len(consolidated) == 2


def test_consolidate_preserves_latest_timestamp():
    """Test that consolidation updates timestamp."""
    items = [
        {
            "id": "item1",
            "product_id": "12345",
            "quantity": 2,
            "last_updated": "2024-01-01T10:00:00"
        },
        {
            "id": "item2",
            "product_id": "12345",
            "quantity": 3,
            "last_updated": "2024-01-01T11:00:00"
        }
    ]

    consolidated = _consolidate_items(items)

    assert len(consolidated) == 1
    assert "last_updated" in consolidated[0]
    # Should have a new timestamp from consolidation
