"""Rate-limit resilience for the Kroger API (429 / 5xx backoff).

Ownership: wraps the third-party ``kroger_api.client.KrogerClient`` HTTP
chokepoints so EVERY Kroger call — product search, location, cart, identity,
and the OAuth token grants/refreshes — retries transient failures with
exponential backoff. The library issues bare ``requests.<verb>`` calls (no
shared ``Session``), so a mounted ``HTTPAdapter`` retry is not an option;
monkeypatching the two methods that funnel all traffic is the single seam that
covers the whole surface.

``install_kroger_retry()`` is idempotent and is invoked at ``tools/shared.py``
import time, so it is active before any client is built.

WHY monkeypatch rather than per-call wrappers: there are ~40 call sites across
the MCP tools and web routes; a chokepoint wrapper guarantees uniform coverage
and cannot be forgotten at a new call site.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import requests
from kroger_api.client import KrogerClient

logger = logging.getLogger(__name__)

# HTTP statuses worth retrying: explicit rate-limit + transient upstream errors.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 0.5
BACKOFF_FACTOR = 2.0
MAX_DELAY_SECONDS = 16.0

_INSTALLED_FLAG = "_smartshopper_retry_installed"


def _retry_after_seconds(exc: requests.exceptions.HTTPError) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds form) from a 429/503, if present."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        # Kroger sends delta-seconds; clamp to our ceiling to bound a hostile value.
        return min(float(int(raw.strip())), MAX_DELAY_SECONDS)
    except (ValueError, TypeError):
        # HTTP-date form is not emitted by Kroger; fall back to computed backoff.
        return None


def _status_of(exc: requests.exceptions.HTTPError) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def _sleep_for(attempt: int, retry_after: float | None) -> float:
    """Compute the backoff sleep for a 1-indexed attempt, honoring Retry-After."""
    if retry_after is not None:
        return retry_after
    delay = BASE_DELAY_SECONDS * (BACKOFF_FACTOR ** (attempt - 1))
    delay = min(delay, MAX_DELAY_SECONDS)
    # Full jitter on the computed component avoids synchronized retry storms
    # when many workers are throttled at once.
    return delay + random.uniform(0, delay * 0.25)


def _with_retry(method: Callable[..., Any], op_name: str) -> Callable[..., Any]:
    """Return a wrapper that retries ``method`` on retryable HTTP/connection errors."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return method(*args, **kwargs)
            except requests.exceptions.HTTPError as exc:
                status = _status_of(exc)
                if status not in RETRYABLE_STATUS:
                    raise
                last_exc = exc
                retry_after = _retry_after_seconds(exc)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_exc = exc
                retry_after = None
                status = None

            if attempt == MAX_ATTEMPTS:
                logger.error(
                    "kroger_call_exhausted op=%s status=%s attempts=%d",
                    op_name,
                    status,
                    MAX_ATTEMPTS,
                )
                assert last_exc is not None  # nosec B101 — set in every except above
                raise last_exc
            sleep = _sleep_for(attempt, retry_after)
            logger.warning(
                "kroger_rate_limited op=%s status=%s attempt=%d sleep=%.2fs",
                op_name,
                status,
                attempt,
                sleep,
            )
            time.sleep(sleep)
        # Unreachable: the loop either returns, raises on non-retryable, or
        # re-raises last_exc on the final attempt.
        raise AssertionError("retry loop fell through")  # pragma: no cover

    return wrapper


def install_kroger_retry() -> None:
    """Idempotently wrap ``KrogerClient`` HTTP methods with retry/backoff.

    Safe to call repeatedly: a sentinel attribute guards against double-wrapping
    (which would multiply attempts). Wraps both resource calls (``_make_request``)
    and token grants/refreshes (``_get_token``).
    """
    if getattr(KrogerClient, _INSTALLED_FLAG, False):
        return
    KrogerClient._make_request = _with_retry(KrogerClient._make_request, "make_request")
    KrogerClient._get_token = _with_retry(KrogerClient._get_token, "get_token")
    setattr(KrogerClient, _INSTALLED_FLAG, True)
    logger.info("kroger_retry_installed max_attempts=%d", MAX_ATTEMPTS)
