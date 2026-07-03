"""Efficiency batch: order-history N+1 fix + read-through caches.

Critical path: the N+1 fix is a data-integrity guarantee (order history must
return the SAME grouped items, just in fewer queries). The cache tests pin that
hot reads memoize and that distinct inputs don't collide.
"""

from __future__ import annotations

import kroger_mcp.analytics.purchase_tracker as purchase_tracker
import kroger_mcp.analytics.recipe_cost as recipe_cost
import kroger_mcp.analytics.recipe_scoring as recipe_scoring
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
    import kroger_mcp.analytics.favorites as favorites

    # Patch the consumer's cache_read_through (not cache.get_redis) so the test is
    # immune to suite modules that reload cache out from under a get_redis patch.
    monkeypatch.setattr(favorites, "cache_read_through", _local_cache_read_through({}))
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


# --- recipe scoring cache -----------------------------------------------------


def _recipe(name="basil", pid=None, servings=2):
    ing = {"name": name, "quantity": 1, "unit": "bunch"}
    if pid:
        ing["product_id"] = pid
    return {"id": "r1", "name": "Test", "servings": servings, "ingredients": [ing]}


def _patch_health_compute(monkeypatch, calls):
    def _fake(recipe, names_only=False):
        calls["n"] += 1
        return {"score": 80, "grade": "B", "flags": []}

    monkeypatch.setattr(recipe_scoring, "_calculate_health_score_uncached", _fake)


def test_health_score_second_call_served_from_cache(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    calls = {"n": 0}
    _patch_health_compute(monkeypatch, calls)

    a = recipe_scoring.calculate_health_score(_recipe(), names_only=True)
    b = recipe_scoring.calculate_health_score(_recipe(), names_only=True)
    assert a == b
    assert calls["n"] == 1


def test_health_score_ingredient_edit_changes_key(monkeypatch):
    """Content-addressed keys: editing an ingredient must miss, not serve stale."""
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    calls = {"n": 0}
    _patch_health_compute(monkeypatch, calls)

    recipe_scoring.calculate_health_score(_recipe(name="basil"))
    recipe_scoring.calculate_health_score(_recipe(name="tomato"))
    recipe_scoring.calculate_health_score(_recipe(name="basil", pid="0001"))
    recipe_scoring.calculate_health_score(_recipe(name="basil", servings=6))
    assert calls["n"] == 4  # name, product link, and servings all key the hash


def test_health_score_names_only_variants_do_not_collide(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    calls = {"n": 0}
    _patch_health_compute(monkeypatch, calls)

    recipe_scoring.calculate_health_score(_recipe(), names_only=True)
    recipe_scoring.calculate_health_score(_recipe(), names_only=False)
    assert calls["n"] == 2


def test_health_score_ingredients_version_bump_misses(monkeypatch):
    """Safety-list edits bump ingredients:version → next read recomputes."""
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    calls = {"n": 0}
    _patch_health_compute(monkeypatch, calls)

    recipe_scoring.calculate_health_score(_recipe())
    fake.store["ingredients:version"] = "7"
    recipe_scoring.calculate_health_score(_recipe())
    assert calls["n"] == 2


def test_recipe_cost_cached_and_location_scoped(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    calls = {"n": 0}

    def _fake_cost(recipe, location_id=None, include_spices=False):
        calls["n"] += 1
        return {"total_cost": 5.0, "cost_per_serving": 2.5, "breakdown": []}

    monkeypatch.setattr(recipe_cost, "_estimate_recipe_cost_uncached", _fake_cost)

    a = recipe_cost.estimate_recipe_cost(_recipe(), location_id="03400014")
    b = recipe_cost.estimate_recipe_cost(_recipe(), location_id="03400014")
    assert a == b
    assert calls["n"] == 1

    recipe_cost.estimate_recipe_cost(_recipe(), location_id=None)
    assert calls["n"] == 2  # different location scope → distinct key

    recipe_cost.estimate_recipe_cost(_recipe(), location_id="03400014", include_spices=True)
    assert calls["n"] == 3  # spice mode is part of the key → distinct entry, recomputes


def test_recipe_cost_redis_down_degrades_to_compute(monkeypatch):
    monkeypatch.setattr(cache_mod, "get_redis", lambda: None)
    calls = {"n": 0}

    def _fake_cost(recipe, location_id=None, include_spices=False):
        calls["n"] += 1
        return {"total_cost": 1.0, "cost_per_serving": 0.5, "breakdown": []}

    monkeypatch.setattr(recipe_cost, "_estimate_recipe_cost_uncached", _fake_cost)

    recipe_cost.estimate_recipe_cost(_recipe())
    recipe_cost.estimate_recipe_cost(_recipe())
    assert calls["n"] == 2  # no Redis → every call computes, nothing breaks


# --- upcoming-holidays cache (date-keyed) -----------------------------------


def _local_cache_read_through(store: dict):
    """Stand-in for cache_read_through backed by a per-test dict.

    Patched onto ``seasonal`` so the test pins seasonal's own caching contract
    (date-scoped key + producer delegation) without depending on the shared
    Redis singleton, which other suite modules reload out from under a
    ``cache.get_redis`` monkeypatch.
    """

    def _crt(key, ttl_seconds, producer):
        if key in store:
            return store[key]
        value = producer()
        store[key] = value
        return value

    return _crt


def test_upcoming_holidays_cached_within_day(monkeypatch):
    import datetime as _dt

    import kroger_mcp.analytics.seasonal as seasonal

    store: dict = {}
    monkeypatch.setattr(seasonal, "cache_read_through", _local_cache_read_through(store))

    calls = {"n": 0}
    real = seasonal._get_upcoming_holidays_uncached

    def _counting(days_ahead=30):
        calls["n"] += 1
        return real(days_ahead)

    monkeypatch.setattr(seasonal, "_get_upcoming_holidays_uncached", _counting)

    a = seasonal.get_upcoming_holidays(30)
    b = seasonal.get_upcoming_holidays(30)

    assert a == b
    assert calls["n"] == 1  # second call served from cache
    # Key is date-scoped so it rolls over at midnight.
    today = _dt.date.today().isoformat()
    assert any(today in k for k in store)


def test_upcoming_holidays_distinct_window_misses(monkeypatch):
    import kroger_mcp.analytics.seasonal as seasonal

    store: dict = {}
    monkeypatch.setattr(seasonal, "cache_read_through", _local_cache_read_through(store))

    calls = {"n": 0}
    real = seasonal._get_upcoming_holidays_uncached

    def _counting(days_ahead=30):
        calls["n"] += 1
        return real(days_ahead)

    monkeypatch.setattr(seasonal, "_get_upcoming_holidays_uncached", _counting)

    seasonal.get_upcoming_holidays(30)
    seasonal.get_upcoming_holidays(60)  # different days_ahead → distinct key
    assert calls["n"] == 2


# --- predictions in-process memo --------------------------------------------


def test_predictions_for_period_memoized(monkeypatch):
    import kroger_mcp.analytics.predictions as predictions

    predictions._predictions_memo.clear()
    calls = {"n": 0}

    def _fake_compute(
        days_ahead=14, category_filter=None, min_confidence=0.0, include_overdue=True, user_id=None
    ):
        calls["n"] += 1
        return [
            predictions.RepurchasePrediction(
                "p1", "Milk", "routine", None, 3, 0.5, "medium", 0.8, None, 7.0
            )
        ]

    monkeypatch.setattr(predictions, "_compute_predictions_for_period", _fake_compute)

    a = predictions.get_predictions_for_period(days_ahead=14)
    b = predictions.get_predictions_for_period(days_ahead=14)
    assert a == b
    assert calls["n"] == 1  # second call served from memo

    # Returned list is a fresh copy — mutating it must not poison the memo.
    a.append("x")
    c = predictions.get_predictions_for_period(days_ahead=14)
    assert len(c) == 1

    predictions.get_predictions_for_period(days_ahead=30)  # distinct params recompute
    assert calls["n"] == 2

    predictions._predictions_memo.clear()
