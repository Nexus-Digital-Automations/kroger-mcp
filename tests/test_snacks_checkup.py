"""Tests for the snack replenishment check-up heuristic.

Covers the pre-cart "which snacks likely need replenishing" logic in
analytics.favorites.check_snacks: a snack is pre-ticked when pantry-low,
never ordered, or stale past its typical gap; otherwise it is shown unticked.
Also covers the auto-provisioned Snacks list and the order-stamping path that
feeds the staleness signal.
"""

import os
from datetime import datetime, timedelta

import pytest

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_cursor,
    reset_initialization,
)
from kroger_mcp.analytics.favorites import (
    SNACK_DEFAULT_GAP_DAYS,
    check_snacks,
    get_lists,
    get_snacks_list_ids,
    mark_snacks_ordered,
)

USER = os.environ["KROGER_MCP_DEFAULT_USER_ID"]
SNACK_LIST = "TESTLIST_snacks"


def _days_ago_iso(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


def _seed_snack(product_id, description, *, last_ordered_at=None, typical_gap_days=None,
                pantry_level=None):
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_list_items "
            "(list_id, product_id, description, default_quantity, "
            " last_ordered_at, typical_gap_days) VALUES (?, ?, ?, ?, ?, ?)",
            (SNACK_LIST, product_id, description, 1, last_ordered_at, typical_gap_days),
        )
        if pantry_level is not None:
            cursor.execute(
                "INSERT OR REPLACE INTO pantry_items "
                "(product_id, description, level_percent, user_id) VALUES (?, ?, ?, ?)",
                (product_id, description, pantry_level, USER),
            )


def _candidate(result, product_id):
    return next(c for c in result["candidates"] if c["product_id"] == product_id)


def _cleanup():
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM favorite_list_items WHERE list_id = ?", (SNACK_LIST,))
        cursor.execute("DELETE FROM favorite_lists WHERE id = ?", (SNACK_LIST,))
        cursor.execute(
            "DELETE FROM pantry_items WHERE product_id LIKE 'SNACK_%'",
        )


@pytest.fixture(scope="function")
def snacks_list():
    reset_initialization()
    ensure_initialized()
    _cleanup()
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_lists (id, name, list_type, user_id) "
            "VALUES (?, ?, 'snacks', ?)",
            (SNACK_LIST, "Test Snacks", USER),
        )
    yield
    _cleanup()
    reset_initialization()


def test_snacks_list_is_auto_provisioned():
    """get_lists/get_snacks_list_ids create a built-in snacks list on demand."""
    reset_initialization()
    ensure_initialized()
    try:
        ids = get_snacks_list_ids(USER)
        assert len(ids) >= 1
        lists = get_lists(USER)
        assert any(lst["list_type"] == "snacks" for lst in lists)
    finally:
        reset_initialization()


def test_pantry_low_snack_is_pre_ticked(snacks_list):
    _seed_snack("SNACK_low", "Low Chips", last_ordered_at=_days_ago_iso(1), pantry_level=10)
    result = check_snacks(USER)
    cand = _candidate(result, "SNACK_low")
    assert cand["pre_ticked"] is True
    assert "Pantry at 10%" in cand["reason"]


def test_never_ordered_snack_is_pre_ticked(snacks_list):
    _seed_snack("SNACK_new", "New Nuts", last_ordered_at=None)
    cand = _candidate(check_snacks(USER), "SNACK_new")
    assert cand["never_ordered"] is True
    assert cand["pre_ticked"] is True


def test_stale_snack_past_typical_gap_is_pre_ticked(snacks_list):
    _seed_snack("SNACK_stale", "Stale Popcorn", last_ordered_at=_days_ago_iso(40))
    cand = _candidate(check_snacks(USER), "SNACK_stale")
    assert cand["typical_gap_days"] == SNACK_DEFAULT_GAP_DAYS
    assert cand["days_since_ordered"] >= SNACK_DEFAULT_GAP_DAYS
    assert cand["pre_ticked"] is True


def test_fresh_well_stocked_snack_is_not_pre_ticked(snacks_list):
    _seed_snack("SNACK_fresh", "Fresh Bars", last_ordered_at=_days_ago_iso(3), pantry_level=80)
    cand = _candidate(check_snacks(USER), "SNACK_fresh")
    assert cand["pre_ticked"] is False


def test_per_item_typical_gap_overrides_default(snacks_list):
    """A 5-day gap flags an item bought 7 days ago that the 21-day default would not."""
    _seed_snack("SNACK_gap", "Quick Snack", last_ordered_at=_days_ago_iso(7), typical_gap_days=5)
    cand = _candidate(check_snacks(USER), "SNACK_gap")
    assert cand["typical_gap_days"] == 5
    assert cand["pre_ticked"] is True


def test_mark_snacks_ordered_stamps_last_ordered_at(snacks_list):
    _seed_snack("SNACK_buy", "Buy Me", last_ordered_at=None)
    stamped = mark_snacks_ordered(["SNACK_buy"], USER)
    assert stamped == 1
    cand = _candidate(check_snacks(USER), "SNACK_buy")
    assert cand["never_ordered"] is False
    assert cand["days_since_ordered"] == 0
    assert cand["pre_ticked"] is False
