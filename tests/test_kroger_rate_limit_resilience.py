"""Rate-limit resilience: 429/5xx backoff chokepoint + read-through caching.

Critical path: these guards stand between concurrent users and Kroger's shared
rate bucket. A regression here causes user-visible 429 failures or stale-cache
cross-tenant leaks, so the behavior is pinned explicitly.
"""

from __future__ import annotations

import requests

import kroger_mcp.cache as cache_mod
import kroger_mcp.tools._kroger_retry as retry

# Call cache_read_through AND patch get_redis through the same module object so a
# mid-suite module reload elsewhere can't make the call and the patch diverge
# (which would let the dev box's real Redis answer and break the None-path test).
cache_read_through = cache_mod.cache_read_through


def _http_error(status: int, retry_after: str | None = None) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return requests.exceptions.HTTPError(response=response)


def _no_sleep(monkeypatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


# --- retry chokepoint -------------------------------------------------------


def test_retries_then_succeeds_on_transient_429(monkeypatch):
    sleeps = _no_sleep(monkeypatch)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429)
        return "ok"

    wrapped = retry._with_retry(flaky, "test")
    assert wrapped() == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept before attempts 2 and 3


def test_persistent_429_raises_after_max_attempts(monkeypatch):
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def always_429():
        calls["n"] += 1
        raise _http_error(429)

    wrapped = retry._with_retry(always_429, "test")
    with __import__("pytest").raises(requests.exceptions.HTTPError):
        wrapped()
    assert calls["n"] == retry.MAX_ATTEMPTS


def test_non_retryable_status_raises_immediately(monkeypatch):
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def not_found():
        calls["n"] += 1
        raise _http_error(404)

    wrapped = retry._with_retry(not_found, "test")
    with __import__("pytest").raises(requests.exceptions.HTTPError):
        wrapped()
    assert calls["n"] == 1  # 404 is not retried


def test_retry_after_header_is_honored(monkeypatch):
    sleeps = _no_sleep(monkeypatch)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(503, retry_after="7")
        return "ok"

    wrapped = retry._with_retry(flaky, "test")
    assert wrapped() == "ok"
    assert sleeps == [7.0]  # honored the header exactly, not computed backoff


def test_install_is_idempotent():
    from kroger_api.client import KrogerClient

    retry.install_kroger_retry()
    first = KrogerClient._make_request
    retry.install_kroger_retry()
    # Second install must not re-wrap (which would multiply attempts).
    assert KrogerClient._make_request is first


# --- read-through cache -----------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.gets = 0

    def get(self, key):
        self.gets += 1
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


def test_cache_hit_skips_producer(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    producer_calls = {"n": 0}

    def producer():
        producer_calls["n"] += 1
        return {"data": [1, 2, 3]}

    first = cache_read_through("k:1", 60, producer)
    second = cache_read_through("k:1", 60, producer)
    assert first == second == {"data": [1, 2, 3]}
    assert producer_calls["n"] == 1  # second call served from cache


def test_distinct_keys_do_not_collide(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)
    a = cache_read_through("client:A:term", 60, lambda: {"who": "A"})
    b = cache_read_through("client:B:term", 60, lambda: {"who": "B"})
    assert a == {"who": "A"} and b == {"who": "B"}  # no cross-tenant leak


def test_redis_down_degrades_to_producer(monkeypatch):
    monkeypatch.setattr(cache_mod, "get_redis", lambda: None)
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return "live"

    assert cache_read_through("k", 60, producer) == "live"
    assert cache_read_through("k", 60, producer) == "live"
    assert calls["n"] == 2  # no cache, producer each time


def test_producer_failure_is_not_cached(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "get_redis", lambda: fake)

    def boom():
        raise RuntimeError("kroger down")

    import pytest

    with pytest.raises(RuntimeError):
        cache_read_through("k:fail", 60, boom)
    assert "k:fail" not in fake.store  # failures never memoized
