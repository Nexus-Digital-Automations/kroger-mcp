"""
Consent domain — opt-in governance for the shareable / aggregate analytics layer.

Owns:
  - The catalog of consent CATEGORIES (what may be shared, in plain language).
  - Read/write of per-user consent flags in the ``user_settings`` table, reusing
    the preference accessors in ``kroger_mcp.tools.shared``.
  - ``consent_allows()`` — the single gate the (future) aggregation pipeline must
    call before any user-derived signal leaves the local database.

Does NOT own:
  - First-party analytics (purchase_events, pantry, deals). Those stay local and
    are never gated here — declining consent never degrades the core app.
  - The aggregation / anonymization pipeline itself (Phase 2, not built yet).

State model: consent is opt-in. Every category defaults to ``False`` until the
user explicitly enables it. ``decided`` only flips ``True`` once the user saves a
choice (or withdraws). An undecided user behaves exactly like all-off; the only
difference is that the UI may re-prompt an undecided user but not a decided one.

@stable
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Bump when the wording/scope of CATEGORIES changes materially — a user who
# consented under an older version can then be re-prompted.
POLICY_VERSION = "2026-06-04"

# Keys under which consent lives in the user_settings table. Per-category flags
# are stored as ``consent_<category>``.
_FLAG_PREFIX = "consent_"
_DECIDED_KEY = "consent_decided"
_DECIDED_AT_KEY = "consent_decided_at"
_POLICY_VERSION_KEY = "consent_policy_version"

# The catalog. Descriptions are user-facing and deliberately state both what is
# shared and what is NOT — no names, baskets, recipes, or timestamps.
CATEGORIES: list[dict[str, str]] = [
    {
        "key": "purchase_patterns",
        "label": "Purchase patterns",
        "description": (
            "What kinds of items get bought and how often — as de-identified "
            "frequencies only. Never your name, store account, or individual baskets."
        ),
    },
    {
        "key": "price_observations",
        "label": "Price observations",
        "description": (
            "Shelf and sale prices seen for products. Prices aren't personal to you; "
            "sharing them sharpens deal detection in aggregate."
        ),
    },
    {
        "key": "consumption",
        "label": "Consumption & pantry trends",
        "description": (
            "How quickly common staples get used up, as anonymous rates — never your "
            "pantry contents or the times you cook."
        ),
    },
    {
        "key": "recipe_trends",
        "label": "Recipe trends",
        "description": (
            "Which cuisines and ingredient pairings are popular, as counts only — "
            "never your saved recipes or notes."
        ),
    },
]

_CATEGORY_KEYS = {category["key"] for category in CATEGORIES}


def _flag_key(category: str) -> str:
    return f"{_FLAG_PREFIX}{category}"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_consent(user_id: str) -> dict[str, Any]:
    """Return this user's consent state; every category defaults to disabled.

    Shape::

        {
          "decided": bool, "decided_at": str | None, "policy_version": str,
          "categories": {key: {"enabled": bool, "label": str, "description": str}},
        }
    """
    from kroger_mcp.tools.shared import _load_preferences

    prefs = _load_preferences(user_id=user_id)
    categories = {
        category["key"]: {
            "enabled": bool(prefs.get(_flag_key(category["key"]), False)),
            "label": category["label"],
            "description": category["description"],
        }
        for category in CATEGORIES
    }
    return {
        "decided": bool(prefs.get(_DECIDED_KEY, False)),
        "decided_at": prefs.get(_DECIDED_AT_KEY),
        "policy_version": prefs.get(_POLICY_VERSION_KEY) or POLICY_VERSION,
        "categories": categories,
    }


def set_consent(updates: dict[str, bool], user_id: str) -> dict[str, Any]:
    """Apply per-category opt-in choices and mark consent as decided.

    ``updates`` maps category keys to booleans; omitted categories keep their
    current value. Raises ``KeyError`` (fail-closed) if any key is unknown, so a
    typo can never silently enable or skip a category.
    """
    from kroger_mcp.tools.shared import _save_preference

    unknown = set(updates) - _CATEGORY_KEYS
    if unknown:
        logger.warning(
            "consent_update_rejected",
            extra={
                "event": "consent_update_rejected",
                "context": {"unknown_categories": sorted(unknown)},
            },
        )
        raise KeyError(f"Unknown consent categories: {sorted(unknown)}")

    for category, enabled in updates.items():
        _save_preference(_flag_key(category), bool(enabled), user_id=user_id)

    _save_preference(_DECIDED_KEY, True, user_id=user_id)
    _save_preference(_DECIDED_AT_KEY, _utc_now_iso(), user_id=user_id)
    _save_preference(_POLICY_VERSION_KEY, POLICY_VERSION, user_id=user_id)

    state = get_consent(user_id=user_id)
    logger.info(
        "consent_updated",
        extra={
            "event": "consent_updated",
            "context": {
                "enabled": [k for k, v in state["categories"].items() if v["enabled"]],
                "policy_version": POLICY_VERSION,
            },
        },
    )
    return state


def withdraw_consent(user_id: str) -> dict[str, Any]:
    """Disable every category while keeping the decision on record."""
    return set_consent({category["key"]: False for category in CATEGORIES}, user_id=user_id)


def consent_allows(category: str, user_id: str) -> bool:
    """The gate: ``True`` only if the user has opted this category in.

    Unknown categories are denied (fail-closed) and logged, so a mistyped
    category name can never leak data.
    """
    if category not in _CATEGORY_KEYS:
        logger.warning(
            "consent_check_unknown_category",
            extra={"event": "consent_check_unknown_category", "context": {"category": category}},
        )
        return False

    from kroger_mcp.tools.shared import _load_preferences

    allowed = bool(_load_preferences(user_id=user_id).get(_flag_key(category), False))
    if not allowed:
        logger.debug(
            "consent_denied",
            extra={"event": "consent_denied", "context": {"category": category}},
        )
    return allowed


def delete_shared_data(user_id: str) -> dict[str, Any]:
    """Withdraw consent and purge any shared-derived rows for this user.

    Phase 1: the aggregation pipeline does not exist yet, so there are no shared
    rows to purge — this withdraws consent and records the request. The purge
    call site lives here so it stays correct the moment Phase 2 lands.
    """
    state = withdraw_consent(user_id=user_id)
    purged_rows = 0  # Phase 2: DELETE FROM shared_events WHERE user_id = ?
    logger.info(
        "consent_data_deletion_requested",
        extra={"event": "consent_data_deletion_requested", "context": {"purged_rows": purged_rows}},
    )
    return {"deleted": True, "purged_rows": purged_rows, "consent": state}
