"""Tests for the /snacks page payload builder.

Covers _snacks_payload, which backs the dedicated Snacks page: it merges the
check_snacks heuristic (reason/pre_ticked/gap) with get_list_items metadata
(times_ordered/notes) by product_id, and is scoped to the owning user.
"""

import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_cursor,
    reset_initialization,
)
from kroger_mcp.analytics.favorites import (
    _ensure_snacks_list_for_user,
    create_list,
    get_list_items,
)
from kroger_mcp.web.routes.api.favorites import AddItemBody, add_item
from kroger_mcp.web.routes.snacks import _snacks_payload


def _request(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"id": user_id}))

USER = os.environ["KROGER_MCP_DEFAULT_USER_ID"]
OTHER_USER = "11111111-1111-1111-1111-111111111111"


def _days_ago_iso(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


def _seed_into(list_id, product_id, description, *, last_ordered_at=None,
               typical_gap_days=None, pantry_level=None, times_ordered=0, notes=None):
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_list_items "
            "(list_id, product_id, description, default_quantity, last_ordered_at, "
            " typical_gap_days, times_ordered, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (list_id, product_id, description, 1, last_ordered_at, typical_gap_days,
             times_ordered, notes),
        )
        if pantry_level is not None:
            cursor.execute(
                "INSERT OR REPLACE INTO pantry_items "
                "(product_id, description, level_percent, user_id) VALUES (?, ?, ?, ?)",
                (product_id, description, pantry_level, USER),
            )


def _cleanup():
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM favorite_list_items WHERE product_id LIKE 'SNACKPAGE_%'")
        cursor.execute("DELETE FROM pantry_items WHERE product_id LIKE 'SNACKPAGE_%'")
        cursor.execute(
            "DELETE FROM favorite_lists WHERE list_type = 'snacks' AND user_id IN (?, ?)",
            (USER, OTHER_USER),
        )
        cursor.execute("DELETE FROM favorite_lists WHERE name LIKE 'SNACKPAGE_%'")


@pytest.fixture(scope="function")
def snacks_db():
    reset_initialization()
    ensure_initialized()
    _cleanup()
    list_id = _ensure_snacks_list_for_user(USER)
    yield list_id
    _cleanup()
    reset_initialization()


def test_payload_merges_heuristic_and_item_metadata(snacks_db):
    list_id = snacks_db
    _seed_into(list_id, "SNACKPAGE_low", "Low Chips", last_ordered_at=_days_ago_iso(1),
               pantry_level=10, times_ordered=4, notes="family size")

    payload = _snacks_payload(USER)

    assert payload["active_page"] == "snacks"
    assert payload["list_id"] == list_id
    item = next(i for i in payload["items"] if i["product_id"] == "SNACKPAGE_low")
    # Heuristic fields come from check_snacks…
    assert item["pre_ticked"] is True
    assert "Pantry at 10%" in item["reason"]
    assert item["level_percent"] == 10
    assert item["level_status"] == "low"
    # …and metadata is merged in from get_list_items.
    assert item["times_ordered"] == 4
    assert item["notes"] == "family size"
    # ticked_map mirrors the pre-tick heuristic for the checkbox seed.
    assert payload["ticked_map"]["SNACKPAGE_low"] is True


def test_fresh_snack_is_not_pre_ticked(snacks_db):
    list_id = snacks_db
    _seed_into(list_id, "SNACKPAGE_fresh", "Fresh Bars", last_ordered_at=_days_ago_iso(3),
               pantry_level=80)

    payload = _snacks_payload(USER)
    item = next(i for i in payload["items"] if i["product_id"] == "SNACKPAGE_fresh")
    assert item["pre_ticked"] is False
    assert payload["ticked_map"]["SNACKPAGE_fresh"] is False


def test_payload_is_user_scoped(snacks_db):
    """A different user's snacks payload never contains this user's items."""
    list_id = snacks_db
    _seed_into(list_id, "SNACKPAGE_mine", "My Snack", last_ordered_at=None)

    other = _snacks_payload(OTHER_USER)
    assert all(i["product_id"] != "SNACKPAGE_mine" for i in other["items"])
    assert other["list_id"] != list_id


def test_new_snack_added_via_picker_lands_on_snacks_list_only(snacks_db):
    """The product picker POSTs to the snacks list's own id, so a new snack
    lands on the Snacks list specifically — not on any other list the user owns."""
    snacks_list_id = snacks_db
    other = create_list(name="SNACKPAGE_other", list_type="custom", user_id=USER)
    other_list_id = other["list_id"]

    result = asyncio.run(
        add_item(
            snacks_list_id,
            AddItemBody(product_id="SNACKPAGE_new", description="New Snack"),
            _request(USER),
        )
    )
    assert result.get("success") is True

    snack_pids = {
        i["product_id"]
        for i in get_list_items(snacks_list_id, user_id=USER).get("items", [])
    }
    other_pids = {
        i["product_id"]
        for i in get_list_items(other_list_id, user_id=USER).get("items", [])
    }
    assert "SNACKPAGE_new" in snack_pids
    assert "SNACKPAGE_new" not in other_pids
