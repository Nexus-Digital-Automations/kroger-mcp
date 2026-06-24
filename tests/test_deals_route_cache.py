"""Route-level cache for GET /api/deals/auto.

The endpoint fans out ~5 Kroger searches per click; the Batch-4 cache must make
a repeat call within the TTL issue ZERO new searches, while still serving live
(uncached) results when Redis is unavailable.
"""

from __future__ import annotations

import asyncio

import kroger_mcp.web.routes.api.deals as deals


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


class _CountingProduct:
    def __init__(self, counter: dict) -> None:
        self._counter = counter

    def search_products(self, term, location_id, limit):  # noqa: ARG002 - signature parity
        self._counter["n"] += 1
        return {"data": []}


class _Inner:
    # The per-search cache_read_through keys on client.client.client_id.
    client_id = "testcid"


class _CountingClient:
    def __init__(self, counter: dict) -> None:
        self.client = _Inner()
        self.product = _CountingProduct(counter)


def _wire(monkeypatch, counter, redis):
    monkeypatch.setattr(deals, "current_user_id", lambda req: "u1")
    monkeypatch.setattr(deals, "get_client_credentials_client", lambda user_id: _CountingClient(counter))
    monkeypatch.setattr(deals, "get_preferred_location_id", lambda user_id=None: "LOC1")
    monkeypatch.setattr(deals, "get_redis", lambda: redis)
    # The per-category search now reads through the shared cache; keep it
    # transparent here so these tests measure the route-level cache only.
    monkeypatch.setattr("kroger_mcp.cache.get_redis", lambda: None)


def test_auto_deals_second_call_issues_no_new_searches(monkeypatch):
    counter = {"n": 0}
    _wire(monkeypatch, counter, _FakeRedis())
    ncat = len(deals._AUTO_CATEGORIES)

    first = asyncio.run(deals.auto_deals(object(), min_savings=5))
    assert first.status_code == 200
    assert counter["n"] == ncat  # one search per category on the cold call

    asyncio.run(deals.auto_deals(object(), min_savings=5))
    assert counter["n"] == ncat  # served from cache: no extra Kroger calls


def test_auto_deals_runs_live_when_redis_unavailable(monkeypatch):
    counter = {"n": 0}
    _wire(monkeypatch, counter, None)
    ncat = len(deals._AUTO_CATEGORIES)

    asyncio.run(deals.auto_deals(object(), min_savings=5))
    asyncio.run(deals.auto_deals(object(), min_savings=5))
    assert counter["n"] == 2 * ncat  # no cache → both calls scan live
