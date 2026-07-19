"""Tests for editing a snack's typical_gap_days via the PATCH route.

Covers the write path the Snacks page uses for inline cadence editing: persist a
new typical_gap_days, clamp out-of-range input to a valid integer >= 1, and scope
writes to the owning user. Mirrors test_favorites_quantity_api.
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
LIST_ID = "TESTLIST_gap_edit"
PID = "TEST_GAP_1"


def _request(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"id": user_id}))


def _seed():
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_lists (id, name, list_type, user_id) "
            "VALUES (?, ?, 'snacks', ?)",
            (LIST_ID, "Gap Edit Snacks", USER),
        )
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_list_items "
            "(list_id, product_id, description, default_quantity, typical_gap_days) "
            "VALUES (?, ?, ?, ?, ?)",
            (LIST_ID, PID, "Test Snack", 1, 21),
        )


def _cleanup():
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM favorite_list_items WHERE list_id = ?", (LIST_ID,))
        cursor.execute("DELETE FROM favorite_lists WHERE id = ?", (LIST_ID,))


def _stored_gap() -> int:
    with get_db_cursor() as cursor:
        row = cursor.execute(
            "SELECT typical_gap_days FROM favorite_list_items "
            "WHERE list_id = ? AND product_id = ?",
            (LIST_ID, PID),
        ).fetchone()
    return row["typical_gap_days"]


@pytest.fixture(scope="function")
def clean_db(tmp_path, monkeypatch):
    """Was previously unisolated — see test_pantry_expiration.py's clean_db."""
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "snacks_gap_edit_test.db"))
    reset_initialization()
    ensure_initialized()
    _cleanup()
    _seed()
    yield
    _cleanup()
    reset_initialization()


def _patch(user_id: str, gap: int):
    return asyncio.run(
        update_item(LIST_ID, PID, UpdateItemBody(typical_gap_days=gap), _request(user_id))
    )


def test_update_persists_new_gap(clean_db):
    resp = _patch(USER, 7)
    assert resp == {"success": True, "typical_gap_days": 7}
    assert _stored_gap() == 7


def test_gap_below_one_is_clamped(clean_db):
    resp = _patch(USER, 0)
    assert resp["typical_gap_days"] == 1
    assert _stored_gap() == 1

    resp = _patch(USER, -5)
    assert resp["typical_gap_days"] == 1
    assert _stored_gap() == 1


def test_update_is_user_scoped(clean_db):
    """A different user cannot edit this snack's gap; the value is unchanged."""
    resp = _patch(OTHER_USER, 3)
    body = json.loads(bytes(resp.body))
    assert resp.status_code == 404
    assert body["success"] is False
    assert _stored_gap() == 21


def test_empty_body_returns_400(clean_db):
    """PATCH with neither field set is a 400 contract error, not a silent no-op."""
    resp = asyncio.run(update_item(LIST_ID, PID, UpdateItemBody(), _request(USER)))
    body = json.loads(bytes(resp.body))
    assert resp.status_code == 400
    assert body["success"] is False
    assert _stored_gap() == 21  # unchanged


def test_both_fields_update_in_one_request(clean_db):
    """A single PATCH can carry default_quantity and typical_gap_days independently."""
    resp = asyncio.run(
        update_item(
            LIST_ID, PID, UpdateItemBody(default_quantity=4, typical_gap_days=9), _request(USER)
        )
    )
    assert resp == {"success": True, "default_quantity": 4, "typical_gap_days": 9}
    assert _stored_gap() == 9
