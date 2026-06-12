"""Step-time extraction and recipe time totals.

Single source of truth for recipe timing (spec: specs/recipe-step-times.md).
The browser never re-implements extraction — pages embed server-computed
annotations and refetch them after edits.

Model:
- Auto-detection parses durations already written in step text. The per-step
  value is the LARGEST duration mentioned (not the sum): "bake 20 minutes,
  rotating after 10" → 20. Ranges take the upper bound. "overnight" → 8 h.
- Explicit overrides live in ``recipe["step_times"]``, keyed by a content
  hash of the step text — reordering keeps an override, editing the text
  intentionally drops it (the time likely changed too).
- Passive (hands-off) steps — marinating, chilling, rising — are detected by
  verb and totalled separately so "total" can read as
  "30 min active · 8 h hands-off".
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Longest plausible single step: anything above this is a parse artifact.
_MAX_STEP_MINUTES = 48 * 60

# `[\s-]*` before the unit accepts both "5 minutes" and "5-minute".
_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:(?:-|–|—|to)\s*(\d+(?:\.\d+)?))?"
    r"[\s-]*(hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
    re.IGNORECASE,
)

_OVERNIGHT_RE = re.compile(r"\bovernight\b", re.IGNORECASE)
_OVERNIGHT_MINUTES = 8 * 60

# Hands-off verbs: if the step is about one of these, the cook isn't working.
_PASSIVE_RE = re.compile(
    r"\b(marinat\w*|rest\w*|chill\w*|rise\b|rising|proof\w*|refrigerat\w*|"
    r"freez\w*|soak\w*|brine\w*|brining|steep\w*|ferment\w*|cur(?:e|ing)\b|"
    r"(?:let|allow)\s+(?:it\s+)?(?:sit|stand|cool)|cool\s+(?:completely|fully|down)|"
    r"overnight)\b",
    re.IGNORECASE,
)

_HOUR_UNITS = ("h", "hr", "hrs", "hour", "hours")


def step_time_key(text: str) -> str:
    """Content-hash key for a step's override entry (whitespace-insensitive)."""
    normalized = " ".join((text or "").split()).lower()
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]


def extract_step_time(text: str) -> dict[str, Any] | None:
    """Auto-detect a duration in step text.

    Returns ``{"minutes": int, "passive": bool}`` or ``None`` when the step
    mentions no usable duration.
    """
    if not text:
        return None

    candidates: list[float] = []
    for m in _DURATION_RE.finditer(text):
        low, high, unit = m.group(1), m.group(2), m.group(3).lower()
        value = float(high) if high else float(low)
        if unit in _HOUR_UNITS:
            value *= 60
        if 0 < value <= _MAX_STEP_MINUTES:
            candidates.append(value)

    if _OVERNIGHT_RE.search(text):
        candidates.append(_OVERNIGHT_MINUTES)

    if not candidates:
        return None

    return {
        "minutes": int(round(max(candidates))),
        "passive": bool(_PASSIVE_RE.search(text)),
    }


def format_minutes(minutes: int | None) -> str:
    """Human label: 45 → "45 min", 70 → "1 h 10 min", 480 → "8 h"."""
    if not minutes or minutes <= 0:
        return ""
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"


def annotate_steps(
    flat_steps: list[str],
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Annotate a flat step list (headers included, as the templates flatten).

    Returns ``{"times": [entry per step], "totals": {...}}``. Each entry
    always carries ``key``; timed entries add
    ``{minutes, passive, source: "auto"|"override", label}``.
    """
    overrides = overrides or {}
    times: list[dict[str, Any]] = []
    active = 0
    passive = 0

    for text in flat_steps:
        key = step_time_key(text)
        entry: dict[str, Any] | None = None
        ov = overrides.get(key)
        if isinstance(ov, dict) and ov.get("minutes"):
            entry = {
                "minutes": int(ov["minutes"]),
                "passive": bool(ov.get("passive")),
                "source": "override",
            }
        else:
            auto = extract_step_time(text)
            if auto:
                entry = {
                    "minutes": auto["minutes"],
                    "passive": auto["passive"],
                    "source": "auto",
                }
        if entry:
            entry["label"] = format_minutes(entry["minutes"])
            if entry["passive"]:
                passive += entry["minutes"]
            else:
                active += entry["minutes"]
            times.append({"key": key, **entry})
        else:
            times.append({"key": key})

    total = active + passive
    return {
        "times": times,
        "totals": {
            "active": active,
            "passive": passive,
            "total": total,
            "active_label": format_minutes(active),
            "passive_label": format_minutes(passive),
            "total_label": format_minutes(total),
        },
    }


def _flatten_instructions(recipe: dict[str, Any]) -> list[str]:
    """Flatten a recipe's instructions text the same way the templates do."""
    # Local import: routes.recipes also imports this module.
    from kroger_mcp.web.routes.recipes import _parse_instructions

    flat: list[str] = []
    for group in _parse_instructions(recipe.get("instructions") or ""):
        if group.get("header"):
            flat.append(group["header"])
        flat.extend(group.get("steps") or [])
    return flat


def recipe_time_summary(recipe: dict[str, Any]) -> dict[str, Any]:
    """Effective time summary for list/calendar surfaces.

    Explicit ``total_time_minutes`` wins over the derived step sum.
    Returns ``{total, active, passive, label, explicit}`` (total may be 0
    when nothing is known).
    """
    annotated = annotate_steps(
        _flatten_instructions(recipe), recipe.get("step_times")
    )
    totals = annotated["totals"]
    explicit = recipe.get("total_time_minutes")
    if isinstance(explicit, int | float) and explicit > 0:
        total = int(explicit)
        is_explicit = True
    else:
        total = totals["total"]
        is_explicit = False
    return {
        "total": total,
        "active": totals["active"],
        "passive": totals["passive"],
        "label": format_minutes(total),
        "explicit": is_explicit,
    }
