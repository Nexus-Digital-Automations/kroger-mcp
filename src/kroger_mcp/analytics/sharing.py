"""
Shareable-event tracer — the one sanctioned door for any user-derived signal that
may later leave the local database for aggregate insights.

Owns:
  - ``record_shareable_event()`` — a consent-gated capture point. When the
    relevant category is not opted in, it is a logged no-op and records nothing.

Does NOT own:
  - The aggregation / anonymization pipeline (Phase 2). Today this records nothing
    even when consent allows — it exists only to prove the gate is wired end to end.

Why now: making the consent gate a real, testable boundary means the Phase-2
pipeline has exactly one entry point, and that entry point is already locked by
default. Anything that wants to share data must come through here.

@internal
"""

from __future__ import annotations

import logging
from typing import Any

from kroger_mcp.analytics.consent import consent_allows

logger = logging.getLogger(__name__)


def record_shareable_event(
    category: str, payload: dict[str, Any], user_id: str
) -> bool:
    """Capture one shareable signal iff the user opted the category in.

    Returns ``True`` when consent allowed the event (Phase 2 will then enqueue it
    for anonymized aggregation), ``False`` when consent gated it out. Never raises
    on a denied event — denial is the expected steady state, not an error.
    """
    if not consent_allows(category, user_id=user_id):
        logger.debug(
            "shareable_event_skipped",
            extra={"event": "shareable_event_skipped", "context": {"category": category}},
        )
        return False

    # Phase 2: enqueue `payload` into the anonymized aggregation buffer here.
    logger.info(
        "shareable_event_accepted",
        extra={"event": "shareable_event_accepted", "context": {"category": category}},
    )
    return True
