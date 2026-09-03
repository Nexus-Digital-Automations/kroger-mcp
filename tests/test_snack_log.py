"""One-call snack log: match → deduct, unmatched → silent record + surfacing.

Critical-path coverage (data integrity): a snack log must deduct the pantry
exactly once and write an auditable purchase event; an unmatched log must never
raise (locked decision: silent) but must be recorded and surfaced via the bell
so nothing is silently lost.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _pg_support import skip_on_pg

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_connection,
    reset_initialization,
)
from kroger_mcp.analytics.favorites import (
    SNACK_CONSUMED_REORDER_THRESHOLD,
    SNACK_LOG_DEDUCT_PERCENT,
    add_to_list,
    check_snacks,
    get_snacks_list_ids,
    log_snack_consumption,
    mark_snacks_ordered,
)
from kroger_mcp.analytics.notifications import list_unmatched_snacks_for_bell
from kroger_mcp.analytics.pantry import add_to_pantry, get_pantry_item

pytestmark = skip_on_pg


def _user() -> str:
    return os.environ["KROGER_MCP_DEFAULT_USER_ID"]


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "snack_log_test.db"))
    reset_initialization()
    ensure_initialized()
    yield
    reset_initialization()


def _snack_events(product_id):
    conn = get_db_connection()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT event_type, source_description FROM purchase_events "
                "WHERE product_id = ? AND event_type = 'snack_consumed'",
                (product_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def _snack_row(product_id):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT last_consumed_at, consumed_count_since_order "
            "FROM favorite_list_items WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── matched path ─────────────────────────────────────────────────────────────

def test_matched_snack_deducts_and_records_event(clean_db):
    add_to_pantry("SN_CHIPS", "Tortilla Chips", level=100, user_id=_user())

    result = log_snack_consumption("chips", user_id=_user())

    assert result["success"] is True
    assert result["matched"] is True
    assert result["product_id"] == "SN_CHIPS"
    level = get_pantry_item("SN_CHIPS", _user())["level_percent"]
    assert level == 100 - SNACK_LOG_DEDUCT_PERCENT
    events = _snack_events("SN_CHIPS")
    assert len(events) == 1
    assert "chips" in events[0]["source_description"].lower()


def test_empty_item_is_rejected(clean_db):
    result = log_snack_consumption("   ", user_id=_user())
    assert result["success"] is False


# ── unmatched path (silent, recorded, surfaced) ──────────────────────────────

def test_unmatched_snack_is_silent_and_surfaced(clean_db):
    result = log_snack_consumption("dragonfruit gel", user_id=_user())

    assert result["success"] is True  # never errors — locked decision
    assert result["matched"] is False

    surfaced = list_unmatched_snacks_for_bell(_user())
    assert len(surfaced) == 1
    assert surfaced[0]["item_text"] == "dragonfruit gel"


# ── consumption signal into check_snacks ─────────────────────────────────────

def test_consumption_counter_increments_and_resets(clean_db):
    add_to_pantry("SN_CHIPS", "Tortilla Chips", level=100, user_id=_user())
    snacks_list = get_snacks_list_ids(_user())[0]
    add_to_list(snacks_list, "SN_CHIPS", "Tortilla Chips", user_id=_user())

    log_snack_consumption("chips", user_id=_user())
    log_snack_consumption("chips", user_id=_user())

    row = _snack_row("SN_CHIPS")
    assert row["consumed_count_since_order"] == 2
    assert row["last_consumed_at"] is not None

    mark_snacks_ordered(["SN_CHIPS"], user_id=_user())
    assert _snack_row("SN_CHIPS")["consumed_count_since_order"] == 0


def test_check_snacks_pre_ticks_at_consumption_threshold(clean_db):
    add_to_pantry("SN_CHIPS", "Tortilla Chips", level=100, user_id=_user())
    snacks_list = get_snacks_list_ids(_user())[0]
    add_to_list(snacks_list, "SN_CHIPS", "Tortilla Chips", user_id=_user())
    # Order first so never_ordered/staleness can't be the pre-tick reason.
    mark_snacks_ordered(["SN_CHIPS"], user_id=_user())

    for _ in range(SNACK_CONSUMED_REORDER_THRESHOLD - 1):
        log_snack_consumption("chips", user_id=_user())
    candidate = next(
        c for c in check_snacks(user_id=_user())["candidates"]
        if c["product_id"] == "SN_CHIPS"
    )
    assert candidate["pre_ticked"] is False

    log_snack_consumption("chips", user_id=_user())  # crosses the threshold
    candidate = next(
        c for c in check_snacks(user_id=_user())["candidates"]
        if c["product_id"] == "SN_CHIPS"
    )
    assert candidate["pre_ticked"] is True
    assert "eaten into" in candidate["reason"].lower()
    assert candidate["consumed_count_since_order"] == SNACK_CONSUMED_REORDER_THRESHOLD
