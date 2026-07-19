"""Scale tier-2: product-detail local read-through + Kroger API call metering.

Read-through (analytics-adjacent, touches products/price_history): a fresh local
product is served with ZERO Kroger calls; a stale price or a full miss refreshes
exactly one product from Kroger and seeds the local mirror. Metering: every call
increments a per-day counter by API family, and a meter DB error never escapes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import kroger_mcp.analytics.api_meter as api_meter
from kroger_mcp.analytics.api_meter import classify_api_family, meter_kroger_call
from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_cursor,
    reset_initialization,
)
from kroger_mcp.tools.product_catalog import (
    _parse_observed_at,
    product_detail_read_through,
)

PID = "TEST_RT_1"
LOC = "03400014"


class _CountingProduct:
    def __init__(self, counter: dict) -> None:
        self._counter = counter

    def get_product(self, product_id, location_id):  # noqa: ARG002 - signature parity
        self._counter["n"] += 1
        return {
            "data": {
                "productId": product_id,
                "description": "Kroger Olive Oil",
                "brand": "KrogerBrand",
                "upc": "0009999",
                "items": [{"price": {"regular": 9.99, "promo": 7.99}}],
            }
        }


class _Inner:
    client_id = "testcid"


class _CountingClient:
    def __init__(self, counter: dict) -> None:
        self.client = _Inner()
        self.product = _CountingProduct(counter)


def _seed_product(description="Local Olive Oil", brand="LocalBrand"):
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO products (product_id, description, brand, upc) "
            "VALUES (?, ?, ?, ?)",
            (PID, description, brand, "0001111"),
        )


def _seed_price(age_seconds: int, regular=5.49, sale=None):
    observed = (datetime.now() - timedelta(seconds=age_seconds)).isoformat()
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO price_history (product_id, regular_price, sale_price, "
            "on_sale, location_id, observed_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (PID, regular, sale, sale is not None, LOC, observed, "test"),
        )


def _price_row_count() -> int:
    with get_db_cursor() as cursor:
        return cursor.execute(
            "SELECT COUNT(*) AS c FROM price_history WHERE product_id = ?", (PID,)
        ).fetchone()["c"]


@pytest.fixture(scope="function")
def clean_db(tmp_path, monkeypatch):
    """Was previously unisolated — see test_pantry_expiration.py's clean_db."""
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "kroger_scale_tier2_test.db"))
    reset_initialization()
    ensure_initialized()
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM price_history WHERE product_id = ?", (PID,))
        cursor.execute("DELETE FROM products WHERE product_id = ?", (PID,))
        cursor.execute("DELETE FROM kroger_api_calls")
    # Redis off: the read-through layer is transparent so we count real Kroger calls.
    monkeypatch.setattr("kroger_mcp.cache.get_redis", lambda: None)
    yield
    reset_initialization()


# ── Read-through ────────────────────────────────────────────────────────────

def test_local_hit_makes_zero_kroger_calls(clean_db):
    _seed_product()
    _seed_price(age_seconds=60)  # fresh
    counter = {"n": 0}
    record = product_detail_read_through(
        _CountingClient(counter), PID, LOC, freshness_seconds=3600
    )
    assert counter["n"] == 0
    assert record["description"] == "Local Olive Oil"
    assert record["items"][0]["price"]["regular"] == 5.49


def test_stale_price_refreshes_one_product(clean_db):
    _seed_product()
    _seed_price(age_seconds=7200)  # 2h old, threshold 1h → stale
    before = _price_row_count()
    counter = {"n": 0}
    record = product_detail_read_through(
        _CountingClient(counter), PID, LOC, freshness_seconds=3600
    )
    assert counter["n"] == 1  # refreshed from Kroger
    assert record["description"] == "Kroger Olive Oil"  # live data, not local
    assert _price_row_count() == before + 1  # new observation written


def test_full_miss_seeds_local_mirror(clean_db):
    counter = {"n": 0}
    record = product_detail_read_through(_CountingClient(counter), PID, LOC)
    assert counter["n"] == 1
    assert record["productId"] == PID
    with get_db_cursor() as cursor:
        meta = cursor.execute(
            "SELECT description FROM products WHERE product_id = ?", (PID,)
        ).fetchone()
    assert meta is not None and meta["description"] == "Kroger Olive Oil"
    assert _price_row_count() == 1  # price observed on the miss


def test_unknown_product_returns_none(clean_db):
    class _Missing(_CountingProduct):
        def get_product(self, product_id, location_id):  # noqa: ARG002
            self._counter["n"] += 1
            return {"data": None}

    counter = {"n": 0}
    client = _CountingClient(counter)
    client.product = _Missing(counter)
    assert product_detail_read_through(client, PID, LOC) is None


def test_metadata_present_but_no_price_falls_through(clean_db):
    _seed_product()  # metadata only, no price_history row
    counter = {"n": 0}
    product_detail_read_through(_CountingClient(counter), PID, LOC)
    assert counter["n"] == 1  # no fresh price → must fetch


# ── observed_at parsing ─────────────────────────────────────────────────────

def test_parse_observed_at_handles_both_separators():
    assert _parse_observed_at("2026-06-24T13:00:00") is not None
    assert _parse_observed_at("2026-06-24 13:00:00") is not None  # Postgres style
    assert _parse_observed_at("not-a-date") is None
    assert _parse_observed_at(None) is None
    # tz-aware input is normalised to naive (no TypeError when subtracted)
    aware = _parse_observed_at("2026-06-24T13:00:00+00:00")
    assert aware is not None and aware.tzinfo is None


# ── Metering ────────────────────────────────────────────────────────────────

def test_classify_api_family():
    assert classify_api_family("/v1/products/123") == "Products"
    assert classify_api_family("/v1/cart/add") == "Cart"
    assert classify_api_family("/v1/locations") == "Locations"
    assert classify_api_family("/v1/connect/oauth2/profile") == "Identity"
    assert classify_api_family(None) == "Other"


def test_meter_increments_per_family_and_outcome(clean_db):
    meter_kroger_call("make_request", "/v1/products/1", "success")
    meter_kroger_call("make_request", "/v1/products/2", "success")
    meter_kroger_call("make_request", "/v1/cart/add", "failure")
    meter_kroger_call("get_token", None, "success")
    with get_db_cursor() as cursor:
        rows = {
            (r["api_family"], r["outcome"]): r["call_count"]
            for r in cursor.execute(
                "SELECT api_family, outcome, call_count FROM kroger_api_calls"
            ).fetchall()
        }
    assert rows[("Products", "success")] == 2
    assert rows[("Cart", "failure")] == 1
    assert rows[("Identity", "success")] == 1


def test_meter_swallows_db_errors(clean_db, monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(api_meter, "get_db_cursor", _boom)
    # Must NOT raise — metering is best-effort and can never block a Kroger call.
    meter_kroger_call("make_request", "/v1/products/1", "success")
