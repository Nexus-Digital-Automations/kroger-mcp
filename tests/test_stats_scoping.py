"""
Per-user isolation tests for purchase stats, predictions, and reports.

Owns: proof that the multi-tenant scoping added to purchase_tracker.py,
statistics.py, predictions.py, and reporting.py actually isolates two
distinct user_ids — companion to test_user_scoping.py, which covers
favorites/pantry/meal-plan/safety.

@stable
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from kroger_mcp.analytics.database import get_db_connection, insert_returning_id
from kroger_mcp.analytics.pantry import add_to_pantry
from kroger_mcp.analytics.predictions import get_predictions_for_period
from kroger_mcp.analytics.purchase_tracker import (
    ensure_product_exists,
    get_order_history,
    get_purchase_events,
    record_order,
)
from kroger_mcp.analytics.reporting import (
    generate_pantry_report,
    generate_patterns_report,
    generate_prediction_accuracy_report,
    generate_spending_report,
)
from kroger_mcp.analytics.statistics import (
    get_all_product_statistics,
    get_product_statistics,
    update_product_stats,
)
from kroger_mcp.auth.passwords import hash_password


def _seed_dated_order(conn, user_id: str, product_id: str, days_ago: int, quantity: int = 1) -> None:
    """Insert one order + matching 'order_placed' purchase_event dated `days_ago` days back.

    Bypasses record_order (which always stamps `datetime.now()`) so tests can
    control event spacing deterministically for consumption-rate math.
    """
    event_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    order_id = insert_returning_id(
        conn,
        "INSERT INTO orders (user_id, placed_at, item_count, total_quantity, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, event_date, 1, quantity, None),
    )
    conn.execute(
        "INSERT INTO purchase_events "
        "(user_id, product_id, quantity, event_type, modality, event_date, event_timestamp, order_id) "
        "VALUES (?, ?, ?, 'order_placed', 'PICKUP', ?, ?, ?)",
        (user_id, product_id, quantity, event_date, event_date, order_id),
    )
    conn.commit()


@pytest.fixture
def two_users():
    """Create two throwaway users + a throwaway product, cleaned up after.

    Teardown deletes any rows owned by either user across every table this
    module writes to, so tests cannot pollute each other or real user data.
    """
    conn = get_db_connection()
    a_id, b_id = str(uuid.uuid4()), str(uuid.uuid4())
    product_id = f"test-product-{uuid.uuid4()}"
    try:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
            (a_id, f"a-{a_id}@test", hash_password("pw"), "userA"),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
            (b_id, f"b-{b_id}@test", hash_password("pw"), "userB"),
        )
        conn.commit()
        yield a_id, b_id, product_id
    finally:
        for table in ("purchase_events", "orders", "product_statistics", "pantry_items"):
            conn.execute(f"DELETE FROM {table} WHERE user_id IN (?, ?)", (a_id, b_id))
        conn.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
        conn.execute("DELETE FROM users WHERE id IN (?, ?)", (a_id, b_id))
        conn.commit()
        conn.close()


class TestPurchaseTrackerScoping:
    def test_record_order_isolates_events_and_history(self, two_users):
        a_id, b_id, product_id = two_users
        record_order([{"product_id": product_id, "quantity": 3, "modality": "PICKUP"}], user_id=a_id)
        record_order([{"product_id": product_id, "quantity": 1, "modality": "DELIVERY"}], user_id=b_id)
        record_order([{"product_id": product_id, "quantity": 1, "modality": "DELIVERY"}], user_id=b_id)

        a_events = get_purchase_events(product_id, user_id=a_id)
        b_events = get_purchase_events(product_id, user_id=b_id)
        assert len(a_events) == 1
        assert len(b_events) == 2
        assert a_events[0]["quantity"] == 3

        a_orders = get_order_history(user_id=a_id)
        b_orders = get_order_history(user_id=b_id)
        assert len(a_orders) == 1
        assert len(b_orders) == 2


class TestStatisticsScoping:
    def test_update_and_get_product_statistics_isolated(self, two_users):
        a_id, b_id, product_id = two_users
        record_order([{"product_id": product_id, "quantity": 2, "modality": "PICKUP"}], user_id=a_id)
        record_order([{"product_id": product_id, "quantity": 5, "modality": "PICKUP"}], user_id=b_id)
        record_order([{"product_id": product_id, "quantity": 5, "modality": "PICKUP"}], user_id=b_id)

        update_product_stats(product_id, user_id=a_id)
        update_product_stats(product_id, user_id=b_id)

        stats_a = get_product_statistics(product_id, user_id=a_id)
        stats_b = get_product_statistics(product_id, user_id=b_id)

        assert stats_a["total_purchases"] == 1
        assert stats_a["total_quantity"] == 2
        assert stats_b["total_purchases"] == 2
        assert stats_b["total_quantity"] == 10

    def test_get_all_product_statistics_isolated(self, two_users):
        a_id, b_id, product_id = two_users
        record_order([{"product_id": product_id, "quantity": 1, "modality": "PICKUP"}], user_id=a_id)
        update_product_stats(product_id, user_id=a_id)

        all_a = get_all_product_statistics(user_id=a_id)
        all_b = get_all_product_statistics(user_id=b_id)

        assert any(s["product_id"] == product_id for s in all_a)
        assert not any(s["product_id"] == product_id for s in all_b)


class TestPredictionsScoping:
    def test_get_predictions_for_period_isolated(self, two_users):
        a_id, b_id, product_id = two_users
        ensure_product_exists(product_id)
        conn = get_db_connection()
        try:
            # User A: purchases 10 days apart, last one 3 days ago.
            _seed_dated_order(conn, a_id, product_id, days_ago=13)
            _seed_dated_order(conn, a_id, product_id, days_ago=3)
            # User B: purchases 5 days apart, last one 4 days ago.
            _seed_dated_order(conn, b_id, product_id, days_ago=9)
            _seed_dated_order(conn, b_id, product_id, days_ago=4)
        finally:
            conn.close()

        update_product_stats(product_id, user_id=a_id)
        update_product_stats(product_id, user_id=b_id)

        preds_a = get_predictions_for_period(user_id=a_id)
        preds_b = get_predictions_for_period(user_id=b_id)

        pred_a = next(p for p in preds_a if p.product_id == product_id)
        pred_b = next(p for p in preds_b if p.product_id == product_id)

        # Each user's prediction reflects only their own consumption rate —
        # if the queries were unscoped, both would see the same blended value.
        assert pred_a.avg_days_between == 10.0
        assert pred_b.avg_days_between == 5.0
        assert not any(p.product_id == product_id for p in preds_b if p is pred_a)


class TestReportingScoping:
    def test_generate_spending_report_isolated(self, two_users):
        a_id, b_id, product_id = two_users
        record_order([{"product_id": product_id, "quantity": 2, "modality": "PICKUP"}], user_id=a_id)
        record_order([{"product_id": product_id, "quantity": 1, "modality": "PICKUP"}], user_id=b_id)
        record_order([{"product_id": product_id, "quantity": 1, "modality": "PICKUP"}], user_id=b_id)

        report_a = generate_spending_report(user_id=a_id)
        report_b = generate_spending_report(user_id=b_id)

        assert report_a["total_items"] == 1
        assert report_b["total_items"] == 2

    def test_generate_patterns_report_isolated(self, two_users):
        a_id, b_id, product_id = two_users
        record_order([{"product_id": product_id, "quantity": 1, "modality": "PICKUP"}], user_id=a_id)
        record_order([{"product_id": product_id, "quantity": 1, "modality": "DELIVERY"}], user_id=b_id)
        record_order([{"product_id": product_id, "quantity": 1, "modality": "DELIVERY"}], user_id=b_id)

        report_a = generate_patterns_report(user_id=a_id)
        report_b = generate_patterns_report(user_id=b_id)

        assert report_a["total_orders"] == 1
        assert report_b["total_orders"] == 2
        assert report_a["by_modality"] == {"PICKUP": 1}
        assert report_b["by_modality"] == {"DELIVERY": 2}

    def test_generate_pantry_report_isolated(self, two_users):
        a_id, b_id, product_id = two_users
        add_to_pantry(product_id, description="Test Product", level=15, user_id=a_id)
        add_to_pantry(product_id, description="Test Product", level=80, user_id=b_id)

        report_a = generate_pantry_report(user_id=a_id)
        report_b = generate_pantry_report(user_id=b_id)

        assert report_a["total_items"] == 1
        assert report_a["status_breakdown"]["low"] == 1
        assert report_b["total_items"] == 1
        assert report_b["status_breakdown"]["good"] == 1

    def test_generate_prediction_accuracy_report_isolated(self, two_users):
        a_id, b_id, product_id = two_users
        ensure_product_exists(product_id)
        conn = get_db_connection()
        try:
            # User A: perfectly regular 10-day cadence (3 events, 2 intervals).
            _seed_dated_order(conn, a_id, product_id, days_ago=20)
            _seed_dated_order(conn, a_id, product_id, days_ago=10)
            _seed_dated_order(conn, a_id, product_id, days_ago=0)
            # User B: perfectly regular 15-day cadence.
            _seed_dated_order(conn, b_id, product_id, days_ago=30)
            _seed_dated_order(conn, b_id, product_id, days_ago=15)
            _seed_dated_order(conn, b_id, product_id, days_ago=0)
        finally:
            conn.close()

        update_product_stats(product_id, user_id=a_id)
        update_product_stats(product_id, user_id=b_id)

        report_a = generate_prediction_accuracy_report(user_id=a_id)
        report_b = generate_prediction_accuracy_report(user_id=b_id)

        assert report_a["total_products_analyzed"] == 1
        assert report_b["total_products_analyzed"] == 1
        assert report_a["high_accuracy"]["products"][0]["avg_days"] == 10.0
        assert report_b["high_accuracy"]["products"][0]["avg_days"] == 15.0
