"""Best-effort per-day metering of Kroger API calls.

Every Kroger HTTP call funnels through ``_with_retry`` in
``tools/_kroger_retry.py``; that wrapper calls :func:`meter_kroger_call` once per
call. We aggregate into ``kroger_api_calls`` as per-day counters keyed by
(date, api_family, op_name, outcome) so we can see where the shared app's
~10,000/day Products budget goes and produce usage numbers to justify a higher
Kroger rate tier.

Contract: metering is **best-effort**. Any failure here (DB down, not yet
initialized, lock contention) is swallowed — it must NEVER raise or block a
Kroger call. A dropped increment is acceptable; the numbers are directional.
"""

import json
import logging
from datetime import date

from .database import ensure_initialized, get_db_cursor

logger = logging.getLogger(__name__)


def classify_api_family(endpoint: str | None) -> str:
    """Map a Kroger request path to its rate-limited API family.

    The retry wrapper only knows the op is ``make_request``; the endpoint path
    (e.g. ``/v1/products``) is what distinguishes Products — the budget that
    actually binds — from Locations/Cart/Identity.
    """
    if not endpoint:
        return "Other"
    path = endpoint.lower()
    if "/products" in path:
        return "Products"
    if "/cart" in path:
        return "Cart"
    if "/locations" in path or "/chains" in path or "/departments" in path:
        return "Locations"
    if "/profile" in path or "/oauth2" in path or "/token" in path:
        return "Identity"
    return "Other"


def meter_kroger_call(op_name: str, endpoint: str | None, outcome: str) -> None:
    """Increment the per-day counter for one Kroger API call. Never raises.

    Args:
        op_name: the wrapped method ("make_request" or "get_token").
        endpoint: the request path for make_request calls (None for token grants).
        outcome: "success" or "failure".
    """
    # Token grants/refreshes carry no resource endpoint — classify by op.
    api_family = "Identity" if op_name == "get_token" else classify_api_family(endpoint)
    try:
        ensure_initialized()
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO kroger_api_calls
                    (call_date, api_family, op_name, outcome, call_count, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(call_date, api_family, op_name, outcome)
                DO UPDATE SET
                    call_count = kroger_api_calls.call_count + 1,
                    updated_at = excluded.updated_at
                """,
                (date.today().isoformat(), api_family, op_name, outcome,
                 _now_iso()),
            )
        logger.debug(
            json.dumps(
                {
                    "event": "kroger_api_call",
                    "level": "DEBUG",
                    "context": {
                        "api_family": api_family,
                        "op_name": op_name,
                        "outcome": outcome,
                    },
                }
            )
        )
    except Exception:
        # Best-effort: metering must never break a Kroger call.
        logger.debug("kroger_api_call metering skipped (best-effort)", exc_info=True)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat()
