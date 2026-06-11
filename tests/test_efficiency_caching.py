"""Efficiency batch: order-history N+1 fix + read-through caches.

Critical path: the N+1 fix is a data-integrity guarantee (order history must
return the SAME grouped items, just in fewer queries). The cache tests pin that
hot reads memoize and that distinct inputs don't collide.
"""

from __future__ import annotations

import kroger_mcp.analytics.purchase_tracker as purchase_tracker
import kroger_mcp.analytics.recommendations as recommendations
import kroger_mcp.cache as cache_mod


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


# --- order-history N+1 fix --------------------------------------------------


def test_order_history_fetches_all_items_in_one_query(monkeypatch):
    """Items for N orders load in ONE query, not N — and group correctly."""
    orders = [{"id": 1, "placed_at": "t1"}, {"id": 2, "placed_at": "t2"}]
    items = [
        {"order_id": 1, "product_id": "a"},
        {"order_id": 2, "product_id": "b"},
        {"order_id": 1, "product_id": "c"},
    ]

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Conn:
        def __init__(self):
            self.queries: list[str] = []

        def execute(self, sql, params=()):
            self.queries.append(sql)
            return _Cursor(orders if "FROM orders" in sql else items)

        def close(self):
            pass

    conn = _Conn()
    monkeypatch.setattr(purchase_tracker, "ensure_initialized", lambda: None)
    monkeypatch.setattr(purchase_tracker, "get_db_connection", lambda: conn)

    result = purchase_tracker.get_order_history(limit=10)

    # 1 query for orders + 1 for ALL items — never 1 + N.
    assert len(conn.queries) == 2
    by_id = {o["id"]: o for o in result}
    assert [i["product_id"] for i in by_id[1]["items"]] == ["a", "c"]
    assert [i["product_id"] for i in by_id[2]["items"]] == ["b"]


def test_order_history_empty_returns_without_items_query(monkeypatch):
    """No orders → skip the items query entirely (no empty IN (...))."""

    class _Cursor:
        def fetchall(self):
            return []

    class _Conn:
        def __init__(self):
            self.queries: list[str] = []

        def execute(self, sql, params=()):
            self.queries.append(sql)
            return _Cursor()

        def close(self):
            pass

    conn = _Conn()
    monkeypatch.setattr(purchase_tracker, "ensure_initialized", lambda: None)
    monkeypatch.setattr(purchase_tracker, "get_db_connection", lambda: conn)

    assert purchase_tracker.get_order_history() == []
    assert len(conn.queries) == 1  # only the orders query ran


# --- recommendations cache --------------------------------------------------


def test_recommendations_cached_per_param_set(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    calls = {"n": 0}

    def _fake_compute(**kwargs):
        calls["n"] += 1
        return {"success": True, "summary": {"call": calls["n"]}}

    monkeypatch.setattr(
        recommendations, "_compute_comprehensive_recommendations", _fake_compute
    )

    a = recommendations.get_comprehensive_recommendations(max_results=5)
    b = recommendations.get_comprehensive_recommendations(max_results=5)
    assert a == b
    assert calls["n"] == 1  # second call served from cache

    recommendations.get_comprehensive_recommendations(max_results=10)
    assert calls["n"] == 2  # different params → distinct key → recompute


# --- favorites id-set cache -------------------------------------------------


def test_favorite_ids_cached_and_returned_as_set(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    import kroger_mcp.analytics.favorites as favorites

    monkeypatch.setattr(favorites, "ensure_initialized", lambda: None)
    db_calls = {"n": 0}

    class _Cursor:
        def execute(self, *a):
            db_calls["n"] += 1

        def fetchall(self):
            return [{"product_id": "p1"}, {"product_id": "p2"}]

    class _CursorCtx:
        def __enter__(self):
            return _Cursor()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(favorites, "get_db_cursor", lambda: _CursorCtx())

    first = favorites.get_all_favorite_product_ids()
    second = favorites.get_all_favorite_product_ids()
    assert first == second == {"p1", "p2"}  # returned as a set
    assert db_calls["n"] == 1  # second call served from Redis, no DB hit
