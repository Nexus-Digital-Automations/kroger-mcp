"""Tests for POST /api/favorites/snacks/add-to-list.

Backs the acceptance criterion: ticking snacks on the Snacks page and clicking
"Add ticked to shopping list" appends exactly those snacks to the user's
shopping list. Covers the happy path, the check_snacks allow-list (a forged
product_id is ignored), and user scoping.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_cursor,
    reset_initialization,
)
from kroger_mcp.tools.shopping_list_tools import _load_shopping_list
from kroger_mcp.web.routes.api.favorites import AddSnacksBody, add_snacks_to_shopping_list

USER = os.environ["KROGER_MCP_DEFAULT_USER_ID"]
OTHER_USER = "22222222-2222-2222-2222-222222222222"
SNACK_LIST = "TESTLIST_snacks_add"
PID = "SNACKADD_1"
NAME = "Test Trail Mix"


def _request(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"id": user_id}))


def _seed_snack():
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_lists (id, name, list_type, user_id) "
            "VALUES (?, ?, 'snacks', ?)",
            (SNACK_LIST, "Add-To-List Snacks", USER),
        )
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_list_items "
            "(list_id, product_id, description, default_quantity) VALUES (?, ?, ?, ?)",
            (SNACK_LIST, PID, NAME, 2),
        )


def _cleanup():
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM favorite_list_items WHERE list_id = ?", (SNACK_LIST,))
        cursor.execute("DELETE FROM favorite_lists WHERE id = ?", (SNACK_LIST,))
        cursor.execute("DELETE FROM user_shopping_lists WHERE user_id IN (?, ?)", (USER, OTHER_USER))


@pytest.fixture(scope="function")
def snacks_db():
    reset_initialization()
    ensure_initialized()
    _cleanup()
    _seed_snack()
    yield
    _cleanup()
    reset_initialization()


def _add(user_id: str, product_ids: list[str]):
    return asyncio.run(
        add_snacks_to_shopping_list(AddSnacksBody(product_ids=product_ids), _request(user_id))
    )


def test_ticked_snack_appears_on_shopping_list(snacks_db):
    resp = _add(USER, [PID])
    assert resp["success"] is True
    assert resp["items_added"] == 1

    items = _load_shopping_list(user_id=USER)["items"]
    assert any(i["product_id"] == PID and i["name"] == NAME for i in items)


def test_forged_product_id_is_ignored(snacks_db):
    """The check_snacks snapshot is the allow-list: an id not on a snacks list is dropped."""
    resp = _add(USER, ["NOT_A_REAL_SNACK"])
    assert resp["items_added"] == 0

    items = _load_shopping_list(user_id=USER)["items"]
    assert all(i["product_id"] != "NOT_A_REAL_SNACK" for i in items)


def test_empty_selection_adds_nothing(snacks_db):
    resp = _add(USER, [])
    assert resp["items_added"] == 0
    assert _load_shopping_list(user_id=USER)["items"] == []


def test_other_user_cannot_add_this_users_snack(snacks_db):
    """A different user's allow-list doesn't include this user's snack."""
    resp = _add(OTHER_USER, [PID])
    assert resp["items_added"] == 0
    assert all(i["product_id"] != PID for i in _load_shopping_list(user_id=OTHER_USER)["items"])
