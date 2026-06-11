"""Process-local Redis client for caching and cross-worker coordination.

Ownership: shared cache accessor used by auth/session validation, ingredient
pattern versioning, and (Phase 2) safety/recipe result caches.

Design constraints:
- **Fork-safe**: the client is created lazily on first use *inside* each
  uvicorn worker, never at import time. A Redis client holds a socket that
  must not be shared across a ``fork()``.
- **Degrade gracefully**: every caller treats Redis as a best-effort cache.
  If ``REDIS_URL`` is unreachable, ``get_redis()`` returns ``None`` and the
  caller falls back to its source of truth (the database). Caching must never
  block or fail a request.

Connection URL comes from ``REDIS_URL`` (default ``redis://localhost:6379/0``).
"""

from __future__ import annotations

import logging
import os

import redis

logger = logging.getLogger(__name__)

DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# Per-process singletons. Lazy-initialised inside the worker (fork-safe).
_redis: redis.Redis | None = None
_init_failed = False


def get_redis() -> redis.Redis | None:
    """Return a process-local Redis client, or ``None`` if unavailable.

    The first call pings the server to confirm reachability; on failure it
    logs once and returns ``None`` for the remainder of the process so callers
    can skip the cache without repeatedly paying connection timeouts.
    """
    global _redis, _init_failed

    if _redis is not None:
        return _redis
    if _init_failed:
        return None

    url = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
    except Exception as exc:  # connection refused, auth error, DNS, etc.
        # Degraded mode: cache disabled for this process. WARN, don't raise —
        # the app stays fully functional against the database.
        logger.warning("redis unavailable at %s (%s); caching disabled", url, exc)
        _init_failed = True
        return None

    _redis = client
    logger.info("redis connected url=%s", url)
    return _redis


def bump_version(key: str) -> None:
    """Increment a Redis integer version key (best-effort, never raises).

    Used to invalidate per-worker in-process caches across workers: a worker
    compares its cached version to this key and rebuilds when they differ.
    """
    client = get_redis()
    if client is None:
        return
    try:
        client.incr(key)
    except Exception as exc:
        logger.warning("redis bump_version failed key=%s (%s)", key, exc)


def get_version(key: str) -> int | None:
    """Read a Redis integer version key, or ``None`` if unavailable."""
    client = get_redis()
    if client is None:
        return None
    try:
        val = client.get(key)
        return int(val) if val is not None else 0
    except Exception as exc:
        logger.warning("redis get_version failed key=%s (%s)", key, exc)
        return None
