"""Tests for the shopping list's "Mark Empty" pantry reset."""

import importlib

import pytest
from _pg_support import skip_on_pg

from kroger_mcp.analytics.database import ensure_initialized, reset_initialization
from kroger_mcp.analytics.pantry import add_to_pantry, get_pantry_item
from kroger_mcp.auth.dependencies import default_user_id
from kroger_mcp.tools.shopping_list_tools import _load_shopping_list, _save_shopping_list
from kroger_mcp.web.routes.api.shopping_list import _mark_list_items_empty


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "mark_empty_test.db"))
    reset_initialization()
    ensure_initialized()
    yield default_user_id()
    reset_initialization()


@skip_on_pg
def test_zeroes_tracked_items_and_skips_the_rest(isolated_db):
    user = isolated_db
    add_to_pantry(product_id="p-milk", description="Milk", level=80, user_id=user)
    add_to_pantry(product_id="p-eggs", description="Eggs", level=50, user_id=user)
    add_to_pantry(product_id="p-rice", description="Rice", level=90, user_id=user)
    items = [
        {"id": "a", "product_id": "p-milk", "name": "Milk", "quantity": 1},
        {"id": "b", "product_id": "p-eggs", "name": "Eggs", "quantity": 1},
        {"id": "c", "product_id": "p-untracked", "name": "Kale", "quantity": 1},
        {"id": "d", "product_id": None, "name": "Gochujang", "quantity": 1},
    ]
    _save_shopping_list({"items": items}, user_id=user)

    result = _mark_list_items_empty(user)

    assert result == {"emptied": 2, "skipped": 2}
    assert get_pantry_item("p-milk", user_id=user)["level_percent"] == 0
    assert get_pantry_item("p-eggs", user_id=user)["level_percent"] == 0
    # Not on the list -> untouched; untracked list item -> not added.
    assert get_pantry_item("p-rice", user_id=user)["level_percent"] == 90
    assert get_pantry_item("p-untracked", user_id=user) is None
    # The shopping list itself is kept.
    assert len(_load_shopping_list(user_id=user)["items"]) == 4
