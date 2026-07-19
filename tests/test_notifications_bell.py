"""Regression coverage for the two new notification-bell sections.

Pantry low-stock/expiring items and the "no plan for next week" reminder used
to be computed but never surfaced anywhere except the dashboard page (pantry)
or nowhere at all (meal-plan reminder). Both are now part of the
/api/notifications payload the bell polls -- these tests lock in the
underlying analytics.notifications helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_connection,
    reset_initialization,
)
from kroger_mcp.analytics.meal_planning import create_meal_plan
from kroger_mcp.analytics.notifications import (
    list_pantry_alerts_for_bell,
    next_week_needs_plan,
)
from kroger_mcp.analytics.pantry import add_to_pantry
from kroger_mcp.auth.dependencies import default_user_id


def _user_id() -> str:
    return default_user_id()


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    """Isolated tmp_path-backed DB -- never touches the real dev/prod database."""
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "notifications_bell_test.db"))
    reset_initialization()
    ensure_initialized()
    yield
    reset_initialization()


def _set_expiration(product_id: str, days_out: int) -> None:
    exp_date = (datetime.now() + timedelta(days=days_out)).strftime("%Y-%m-%d")
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE pantry_items SET expiration_date = ? WHERE product_id = ?",
            (exp_date, product_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestPantryAlertsForBell:
    def test_low_stock_item_is_alerted(self, clean_db):
        add_to_pantry(
            "BELL_LOW", description="Low Item", level=10, low_threshold=20,
            user_id=_user_id(),
        )
        alerts = list_pantry_alerts_for_bell(_user_id())
        assert [a["product_id"] for a in alerts] == ["BELL_LOW"]

    def test_expiring_item_is_alerted(self, clean_db):
        add_to_pantry(
            "BELL_EXP", description="Expiring Item", level=100, low_threshold=20,
            user_id=_user_id(),
        )
        _set_expiration("BELL_EXP", days_out=3)
        alerts = list_pantry_alerts_for_bell(_user_id())
        assert [a["product_id"] for a in alerts] == ["BELL_EXP"]

    def test_well_stocked_item_is_not_alerted(self, clean_db):
        add_to_pantry(
            "BELL_OK", description="Fine Item", level=90, low_threshold=20,
            user_id=_user_id(),
        )
        assert list_pantry_alerts_for_bell(_user_id()) == []

    def test_limit_caps_results(self, clean_db):
        for i in range(7):
            add_to_pantry(
                f"BELL_LOW_{i}", description=f"Low Item {i}", level=5,
                low_threshold=20, user_id=_user_id(),
            )
        assert len(list_pantry_alerts_for_bell(_user_id(), limit=5)) == 5


class TestNextWeekNeedsPlan:
    def test_true_when_no_plan_covers_next_monday(self, clean_db):
        assert next_week_needs_plan(_user_id()) is True

    def test_false_once_a_plan_covers_next_monday(self, clean_db):
        today = datetime.now().date()
        next_monday = today + timedelta(days=(7 - today.weekday()))
        next_sunday = next_monday + timedelta(days=6)
        create_meal_plan(
            name="Next week",
            start_date=next_monday.isoformat(),
            end_date=next_sunday.isoformat(),
            user_id=_user_id(),
        )
        assert next_week_needs_plan(_user_id()) is False
