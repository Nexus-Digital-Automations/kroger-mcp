"""Tests for editing a favorite item's default_quantity via the PATCH route.

Covers the write path the favorites detail page uses for inline quantity editing:
persist a new quantity, clamp out-of-range input to a valid integer >= 1, and
scope writes to the owning user.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_cursor,
    reset_initialization,
)
from kroger_mcp.web.routes.api.favorites import UpdateItemBody, update_item

USER = os.environ["KROGER_MCP_DEFAULT_USER_ID"]
OTHER_USER = "00000000-0000-0000-0000-000000000000"
LIST_ID = "TESTLIST_qty_edit"
PID = "TEST_QTY_1"


def _request(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"id": user_id}))


def _seed():
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_lists (id, name, user_id) VALUES (?, ?, ?)",
            (LIST_ID, "Qty Edit List", USER),
        )
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_list_items "
            "(list_id, product_id, description, brand, default_quantity) "
            "VALUES (?, ?, ?, ?, ?)",
            (LIST_ID, PID, "Test Item", "TestBrand", 2),
        )


def _cleanup():
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM favorite_list_items WHERE list_id = ?", (LIST_ID,))
        cursor.execute("DELETE FROM favorite_lists WHERE id = ?", (LIST_ID,))


def _stored_quantity() -> int:
    with get_db_cursor() as cursor:
        row = cursor.execute(
            "SELECT default_quantity FROM favorite_list_items "
            "WHERE list_id = ? AND product_id = ?",
            (LIST_ID, PID),
        ).fetchone()
    return row["default_quantity"]


@pytest.fixture(scope="function")
def clean_db():
    reset_initialization()
    ensure_initialized()
    _cleanup()
    _seed()
    yield
    _cleanup()
    reset_initialization()


def _patch(user_id: str, quantity: int):
    return asyncio.run(
        update_item(LIST_ID, PID, UpdateItemBody(default_quantity=quantity), _request(user_id))
    )


def test_update_persists_new_quantity(clean_db):
    resp = _patch(USER, 5)
    assert resp == {"success": True, "default_quantity": 5}
    assert _stored_quantity() == 5


def test_quantity_below_one_is_clamped(clean_db):
    resp = _patch(USER, 0)
    assert resp["default_quantity"] == 1
    assert _stored_quantity() == 1

    resp = _patch(USER, -3)
    assert resp["default_quantity"] == 1
    assert _stored_quantity() == 1


def test_update_is_user_scoped(clean_db):
    """A different user cannot edit this list's item; quantity is unchanged."""
    resp = _patch(OTHER_USER, 9)
    # update_list_item returns success:false for a list the user doesn't own,
    # surfaced by the route as a 404 JSONResponse.
    body = json.loads(bytes(resp.body))
    assert resp.status_code == 404
    assert body["success"] is False
    assert _stored_quantity() == 2
