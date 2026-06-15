"""
Tests for the comprehensive pantry: absolute quantities, source-attributed
consumption events, and gap reconciliation.

Critical-path coverage (data integrity): when partial fulfillment happens
("recipe needs 2, ordered 1, used 1 from pantry"), the books must balance —
pantry on-hand decrements correctly, last-used attribution is recorded, and
the event log captures the consumption with its recipe linkage.
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
from kroger_mcp.analytics.pantry import (
    add_to_pantry,
    consume_from_pantry,
    create_pending_gap,
    get_pantry_item,
    get_usage_history,
    list_pending_gaps,
    resolve_gap,
    restock_item,
)

# SQLite-specific: uses PRAGMA foreign_keys = OFF for fixture setup.
pytestmark = skip_on_pg


@pytest.fixture
def clean_db():
    reset_initialization()
    ensure_initialized()

    conn = get_db_connection()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM pending_gaps")
        conn.execute("DELETE FROM pantry_items")
        conn.execute("DELETE FROM purchase_events")
        conn.execute("DELETE FROM product_statistics")
        conn.execute("DELETE FROM products")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    finally:
        conn.close()

    yield

    reset_initialization()


class TestQuantityOnHand:
    """add_to_pantry / restock_item now persist absolute units."""

    def test_add_persists_quantity_and_unit(self, clean_db):
        result = add_to_pantry(
            product_id="P-CANS-001",
            description="Black Beans 15oz",
            level=100,
            quantity=4,
            unit="can",
        )
        assert result["success"] is True
        item = get_pantry_item("P-CANS-001")
        assert item["quantity_on_hand"] == 4
        assert item["unit"] == "can"

    def test_restock_with_explicit_quantity(self, clean_db):
        add_to_pantry("P-RICE-001", "Brown Rice", level=10, quantity=0, unit="lb")
        restock_item("P-RICE-001", level=100, quantity=5, unit="lb")
        item = get_pantry_item("P-RICE-001")
        assert item["quantity_on_hand"] == 5
        assert item["unit"] == "lb"

    def test_restock_without_quantity_preserves_prior(self, clean_db):
        add_to_pantry("P-OIL-001", "Olive Oil", level=10, quantity=2, unit="bottle")
        restock_item("P-OIL-001", level=100)  # quantity unspecified
        item = get_pantry_item("P-OIL-001")
        assert item["quantity_on_hand"] == 2  # not reset to None


class TestConsumeFromPantry:
    """consume_from_pantry writes attribution + event_log + decrements units."""

    def test_decrements_quantity_when_units_match(self, clean_db):
        add_to_pantry("P-CANS-002", "Chickpeas", level=80, quantity=3, unit="can")
        result = consume_from_pantry(
            product_id="P-CANS-002",
            quantity=2,
            unit="can",
            source_type="recipe",
            source_description="Hummus",
            recipe_id="r-hummus",
        )
        assert result["success"] is True
        assert result["new_quantity"] == 1.0
        item = get_pantry_item("P-CANS-002")
        assert item["quantity_on_hand"] == 1.0

    def test_does_not_decrement_when_units_mismatch(self, clean_db):
        add_to_pantry("P-OIL-002", "Olive Oil", level=80, quantity=2, unit="bottle")
        consume_from_pantry(
            product_id="P-OIL-002",
            quantity=1,
            unit="tbsp",  # mismatched
            source_type="recipe",
        )
        item = get_pantry_item("P-OIL-002")
        assert item["quantity_on_hand"] == 2  # untouched — safe under mismatch

    def test_records_last_used_attribution(self, clean_db):
        add_to_pantry("P-PASTA-001", "Spaghetti", level=100, quantity=2, unit="box")
        consume_from_pantry(
            product_id="P-PASTA-001",
            quantity=1,
            unit="box",
            source_type="meal_plan",
            source_description="Pasta Carbonara — dinner 2026-05-23",
            recipe_id="r-carbonara",
        )
        item = get_pantry_item("P-PASTA-001")
        assert item["last_used_at"] is not None
        assert "Carbonara" in item["last_used_source"]

    def test_writes_enriched_event_row(self, clean_db):
        add_to_pantry("P-EGGS-001", "Eggs", level=100, quantity=12, unit="each")
        consume_from_pantry(
            product_id="P-EGGS-001",
            quantity=4,
            unit="each",
            source_type="recipe",
            source_description="Carbonara",
            recipe_id="r-carbonara",
            event_type="recipe_consumed",
        )
        events = get_usage_history("P-EGGS-001", days=7)
        consumed = [e for e in events if e["event_type"] == "recipe_consumed"]
        assert len(consumed) == 1
        assert consumed[0]["recipe_id"] == "r-carbonara"
        assert consumed[0]["quantity_delta"] == -4.0
        assert consumed[0]["unit"] == "each"

    def test_unknown_item_returns_failure(self, clean_db):
        result = consume_from_pantry(product_id="missing", quantity=1)
        assert result["success"] is False


class TestGapReconciliation:
    """pending_gaps + resolve_gap implement the 'ordered N-1, used 1 from pantry' flow."""

    def test_create_and_list_gap(self, clean_db):
        add_to_pantry("P-CANS-003", "Black Beans", level=100, quantity=1, unit="can")
        gap_id = create_pending_gap(
            product_id="P-CANS-003",
            needed_quantity=2,
            ordered_quantity=1,
            unit="can",
            recipe_id="r-chili",
            recipe_name="Three-Bean Chili",
            product_description="Black Beans",
        )
        assert gap_id > 0

        open_gaps = list_pending_gaps()
        assert len(open_gaps) == 1
        assert open_gaps[0]["id"] == gap_id
        assert open_gaps[0]["needed_quantity"] == 2
        assert open_gaps[0]["ordered_quantity"] == 1

    def test_pantry_covered_consumes_shortfall(self, clean_db):
        add_to_pantry("P-CANS-004", "Pinto Beans", level=100, quantity=3, unit="can")
        gap_id = create_pending_gap(
            product_id="P-CANS-004",
            needed_quantity=2,
            ordered_quantity=1,
            unit="can",
            recipe_id="r-chili",
            recipe_name="Chili",
            product_description="Pinto Beans",
        )

        result = resolve_gap(gap_id, resolution="pantry_covered")

        assert result["success"] is True
        assert result["shortfall"] == 1.0
        item = get_pantry_item("P-CANS-004")
        assert item["quantity_on_hand"] == 2  # 3 - 1 shortfall

        # Event log records the gap_reconciled consumption
        events = get_usage_history("P-CANS-004", days=7)
        gap_events = [e for e in events if e["event_type"] == "gap_reconciled"]
        assert len(gap_events) == 1
        assert gap_events[0]["recipe_id"] == "r-chili"

        # Gap moves out of the inbox
        assert list_pending_gaps() == []

    def test_user_skipped_leaves_pantry_alone(self, clean_db):
        add_to_pantry("P-CANS-005", "Kidney Beans", level=100, quantity=3, unit="can")
        gap_id = create_pending_gap(
            product_id="P-CANS-005",
            needed_quantity=2,
            ordered_quantity=1,
            unit="can",
        )

        result = resolve_gap(gap_id, resolution="user_skipped")

        assert result["success"] is True
        item = get_pantry_item("P-CANS-005")
        assert item["quantity_on_hand"] == 3  # untouched
        assert list_pending_gaps() == []  # but the gap is closed

    def test_manual_acquired_leaves_pantry_alone(self, clean_db):
        add_to_pantry("P-HERB-001", "Basil", level=100, quantity=1, unit="bunch")
        gap_id = create_pending_gap(
            product_id="P-HERB-001",
            needed_quantity=2,
            ordered_quantity=0,
            unit="bunch",
        )

        result = resolve_gap(gap_id, resolution="manual_acquired")

        assert result["success"] is True
        item = get_pantry_item("P-HERB-001")
        assert item["quantity_on_hand"] == 1
        assert list_pending_gaps() == []

    def test_invalid_resolution_raises(self, clean_db):
        gap_id = create_pending_gap(
            product_id="P-X-001",
            needed_quantity=2,
            ordered_quantity=1,
        )
        with pytest.raises(ValueError):
            resolve_gap(gap_id, resolution="bogus")

    def test_double_resolve_returns_failure(self, clean_db):
        add_to_pantry("P-X-002", "Thing", level=100, quantity=5, unit="each")
        gap_id = create_pending_gap(
            product_id="P-X-002",
            needed_quantity=2,
            ordered_quantity=1,
            unit="each",
        )
        first = resolve_gap(gap_id, resolution="user_skipped")
        assert first["success"] is True

        second = resolve_gap(gap_id, resolution="pantry_covered")
        assert second["success"] is False
