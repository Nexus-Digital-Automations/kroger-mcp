"""Phase 2 performance tests: safety-result cache + batched price writes.

Covers two optimizations:

1. ``check_products_safety_batch`` memoizes per-product safety results in
   Redis so the O(patterns) ``check_product_safety`` scan runs once per
   (product, ruleset, disabled-set) and is served from cache thereafter —
   while degrading gracefully to a live compute when Redis is unavailable.
2. ``record_price_observations`` writes every observation in a single
   transaction (batched equivalent of ``record_price_observation``).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from _pg_support import skip_on_pg

from kroger_mcp.analytics import safety as safety_mod
from kroger_mcp.analytics.ingredients import SafetyResult

# SQLite-specific: exercises sqlite3 directly for batched price writes.
pytestmark = skip_on_pg


# --------------------------------------------------------------------------- #
# Fake Redis — an in-memory stand-in mirroring the subset of the redis API the
# safety cache touches (get / set with ex=...). Lets us assert hit/miss
# behavior without a live server.
# --------------------------------------------------------------------------- #
class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.versions: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """Point the safety cache at an in-memory fake Redis.

    Patches ``get_redis`` (used inside ``_safety_cache_key`` via
    ``cache.get_version`` and in ``_cached_product_safety``) and forces a
    stable ingredients version so the key is deterministic.
    """
    client = _FakeRedis()
    # cache.get_redis is referenced as cache.get_redis inside safety.py.
    monkeypatch.setattr(safety_mod.cache, "get_redis", lambda: client)
    monkeypatch.setattr(
        safety_mod.cache, "get_version", lambda key: 7
    )
    return client


def _spy_check_product_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    """Wrap check_product_safety with a call counter; return the counter list.

    ``counter[0]`` is incremented on each underlying heavy scan.
    """
    counter = [0]
    real = safety_mod.check_product_safety

    def _counting(*args: Any, **kwargs: Any) -> SafetyResult:
        counter[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(safety_mod, "check_product_safety", _counting)
    return counter


# --------------------------------------------------------------------------- #
# (1) Safety batch cache
# --------------------------------------------------------------------------- #
def test_safety_batch_caches_repeated_product(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """Scanning the same product twice computes the heavy result only once."""
    counter = _spy_check_product_safety(monkeypatch)

    product = {
        "product_id": "TESTPERF001",
        "description": "Organic rolled oats, whole grain",
        "brand": "TestBrand",
    }

    first = safety_mod.check_products_safety_batch([product], user_id="perf-user")
    assert counter[0] == 1, "first scan must do the heavy compute"
    assert fake_redis.store, "result must be written to the cache"

    second = safety_mod.check_products_safety_batch([product], user_id="perf-user")
    assert counter[0] == 1, "second scan must be served from Redis, no recompute"

    # The cached result round-trips to an identical SafetyResult shape.
    sr1 = first[0].safety_result
    sr2 = second[0].safety_result
    assert sr1 is not None and sr2 is not None
    assert sr1.to_dict() == sr2.to_dict()
    assert first[0].status == second[0].status


def test_safety_batch_serialization_round_trips_matches(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """A flagged product round-trips its matches/attributes through the cache."""
    _spy_check_product_safety(monkeypatch)

    # A description likely to flag at least one ingredient OR positive
    # attribute. Whatever the live ruleset produces, the cached copy must
    # equal the freshly-computed one field-for-field.
    product = {
        "product_id": "TESTPERF002",
        "description": "Soda with high fructose corn syrup and red 40",
        "brand": "TestBrand",
    }

    fresh = safety_mod.check_products_safety_batch([product], user_id="perf-user")
    cached = safety_mod.check_products_safety_batch([product], user_id="perf-user")

    assert fresh[0].to_dict() == cached[0].to_dict()


def test_safety_batch_graceful_when_redis_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With Redis down (get_redis -> None) the batch still computes results."""
    counter = _spy_check_product_safety(monkeypatch)
    monkeypatch.setattr(safety_mod.cache, "get_redis", lambda: None)
    monkeypatch.setattr(safety_mod.cache, "get_version", lambda key: None)

    product = {
        "product_id": "TESTPERF003",
        "description": "Organic almonds, raw",
        "brand": "TestBrand",
    }

    first = safety_mod.check_products_safety_batch([product], user_id="perf-user")
    second = safety_mod.check_products_safety_batch([product], user_id="perf-user")

    # No cache -> every scan recomputes; results stay valid.
    assert counter[0] == 2
    assert first[0].safety_result is not None
    assert second[0].safety_result is not None
    assert first[0].to_dict() == second[0].to_dict()


def test_safety_batch_survives_redis_get_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising Redis client never propagates into the request path."""
    counter = _spy_check_product_safety(monkeypatch)

    class _BoomRedis:
        def get(self, key: str) -> str | None:
            raise RuntimeError("redis exploded")

        def set(self, key: str, value: str, ex: int | None = None) -> None:
            raise RuntimeError("redis exploded")

    monkeypatch.setattr(safety_mod.cache, "get_redis", lambda: _BoomRedis())
    monkeypatch.setattr(safety_mod.cache, "get_version", lambda key: 1)

    product = {
        "product_id": "TESTPERF004",
        "description": "Plain greek yogurt",
        "brand": "TestBrand",
    }

    result = safety_mod.check_products_safety_batch([product], user_id="perf-user")
    assert counter[0] == 1
    assert result[0].safety_result is not None


# --------------------------------------------------------------------------- #
# (2) Batched price observations
# --------------------------------------------------------------------------- #
@pytest.fixture
def temp_db(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Build a fresh SQLite schema in a temp file isolated from the shared DB.

    Bypasses ``ensure_initialized`` (which can trigger JSON migration paths)
    by pointing ``DB_FILE`` at a temp path and calling the schema builder
    directly.
    """
    from kroger_mcp.analytics import database as db_mod

    db_file = str(tmp_path / "perf_test.db")
    monkeypatch.setattr(db_mod, "DB_FILE", db_file)
    db_mod.reset_initialization()
    db_mod.initialize_database()
    db_mod.run_schema_migrations()
    # deals.py reads DB_FILE at call time (via get_db_connection), so the
    # monkeypatch is honored without reimporting the module.
    yield db_file
    db_mod.reset_initialization()


def test_record_price_observations_batch_inserts_all_rows(temp_db: str) -> None:
    """Every observation in the batch lands as a price_history row."""
    from kroger_mcp.analytics.deals import record_price_observations

    observations = [
        {
            "product_id": f"TESTBATCH{i:03d}",
            "regular_price": 4.99,
            "sale_price": 3.49,
            "location_id": "03400014",
            "source": "web_search",
        }
        for i in range(5)
    ]

    record_price_observations(observations)

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT product_id, regular_price, sale_price, on_sale, "
            "savings_amount, source FROM price_history "
            "WHERE product_id LIKE 'TESTBATCH%' ORDER BY product_id"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 5
    for i, row in enumerate(rows):
        assert row["product_id"] == f"TESTBATCH{i:03d}"
        assert row["regular_price"] == pytest.approx(4.99)
        assert row["sale_price"] == pytest.approx(3.49)
        assert row["on_sale"] == 1
        assert row["savings_amount"] == pytest.approx(1.50)
        assert row["source"] == "web_search"


def test_record_price_observations_empty_is_noop(temp_db: str) -> None:
    """An empty observation list writes nothing and does not raise."""
    from kroger_mcp.analytics.deals import record_price_observations

    record_price_observations([])

    conn = sqlite3.connect(temp_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_record_price_observations_skips_missing_ids(temp_db: str) -> None:
    """Observations missing product_id / location_id are skipped silently."""
    from kroger_mcp.analytics.deals import record_price_observations

    record_price_observations(
        [
            {"product_id": "", "location_id": "03400014", "regular_price": 1.0},
            {"product_id": "TESTSKIP1", "location_id": "", "regular_price": 1.0},
            {
                "product_id": "TESTKEEP1",
                "location_id": "03400014",
                "regular_price": 2.0,
                "sale_price": None,
                "source": "web_search",
            },
        ]
    )

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT product_id FROM price_history ORDER BY product_id"
        ).fetchall()
    finally:
        conn.close()
    assert [r["product_id"] for r in rows] == ["TESTKEEP1"]
