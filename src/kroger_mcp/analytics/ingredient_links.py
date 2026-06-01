"""Per-account ingredient->product link memory and smart suggestions.

Owns the `ingredient_links` table. Every time an account links a recipe
ingredient to a Kroger product — via the web linking popover or the
`recipes(action='link_ingredient')` MCP tool — we record
`(user_id, norm_name, product_id)` and bump `times_linked`. That memory powers:

  1. ``suggest_products_for_ingredient`` — the "your usuals" list and the
     best-guess auto-link shown in the popover, ranked by prior links, then
     purchase history, then pantry / favorites membership, with blocked
     products demoted (the safety signal).
  2. ``get_canonical_name`` — learned name standardization. Because variant
     spellings ("fresh parsley", "parsley") tend to get linked to the *same*
     product, the product_id is the bridge that lets us learn that the account
     usually calls that product "parsley" — with no curated food dictionary.

Name grouping uses only mechanical normalization (``normalize_ingredient_name``)
plus token overlap against the account's own past names. There is intentionally
no built-in ingredient/adjective list: standardization is learned purely from
the user's history, per product requirement.

All reads and writes are scoped by ``user_id``. Recipes themselves remain
global; only this link memory is per-account.

@stable
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from .database import ensure_initialized, get_db_cursor

logger = logging.getLogger("kroger_mcp.ingredient_links")

# Scoring weights for suggestion ranking. Kept as module constants so the
# blend is auditable and tunable in one place.
_W_LINK_FREQ = 0.6  # per prior link, capped
_LINK_FREQ_CAP = 5
_W_RECENT_LINK = 0.5  # linked within _RECENT_DAYS
_W_PURCHASE_FREQ = 0.2  # per recorded purchase, capped
_PURCHASE_CAP = 5
_W_IN_PANTRY = 0.4
_W_IN_FAVORITES = 0.3
_BLOCKED_PENALTY = 0.1  # multiplier — heavy demotion, never a best guess
_RECENT_DAYS = 30
_BEST_GUESS_MIN_SCORE = 1.2
_CANONICAL_MIN_SUPPORT = 2  # need at least this many total links to standardize
_CANONICAL_MIN_CONFIDENCE = 0.5


def normalize_ingredient_name(name: str) -> str:
    """Collapse a free-text ingredient name to a mechanical grouping key.

    Lowercase, strip punctuation, collapse whitespace, and singularize a
    trailing plural on the final word. WHY mechanical-only: the user asked that
    name standardization be learned from their own history, not seeded from a
    curated food dictionary — so we never strip semantic adjectives like
    "fresh" or "organic". Variant forms that mean the same thing are instead
    reconciled through the shared product they get linked to.

    Returns "" for blank/None input.
    """
    if not name:
        return ""
    text = name.strip().lower()
    text = re.sub(r"[^\w\s]", " ", text)  # punctuation -> space
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    words = text.split(" ")
    last = words[-1]
    # Naive de-pluralization: "onions" -> "onion", "tomatoes" -> "tomato".
    if len(last) > 3 and last.endswith("es") and last[-3] in "sxzo":
        words[-1] = last[:-2]
    elif len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        words[-1] = last[:-1]
    return " ".join(words)


def _tokens(norm_name: str) -> set[str]:
    return {t for t in norm_name.split(" ") if t}


def record_link(
    user_id: str,
    raw_name: str,
    product_id: str,
    product_description: str | None = None,
) -> None:
    """Remember that ``user_id`` linked ``raw_name`` to ``product_id``.

    Upserts on ``(user_id, norm_name, product_id)``: first link inserts,
    repeats increment ``times_linked`` and refresh ``raw_name`` /
    ``last_linked_at`` (so the account's latest surface form wins display).

    Best-effort by contract — callers invoke this on the recipe-save / link hot
    path and must not fail the user action if memory recording fails. Logs and
    swallows here so every caller doesn't have to.
    """
    norm = normalize_ingredient_name(raw_name)
    if not user_id or not norm or not product_id:
        logger.debug(
            "skip record_link user=%s norm=%r product=%s", user_id, norm, product_id
        )
        return
    now = datetime.now().isoformat()
    try:
        ensure_initialized()
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ingredient_links
                    (user_id, norm_name, raw_name, product_id,
                     product_description, times_linked, last_linked_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(user_id, norm_name, product_id) DO UPDATE SET
                    times_linked = times_linked + 1,
                    raw_name = excluded.raw_name,
                    product_description = COALESCE(
                        excluded.product_description, product_description),
                    last_linked_at = excluded.last_linked_at
                """,
                (user_id, norm, raw_name.strip(), product_id, product_description, now),
            )
        logger.info(
            "ingredient_link.recorded user=%s norm=%r product=%s", user_id, norm, product_id
        )
    except Exception:
        # Memory is an enhancement; never break the link/save it rode in on.
        logger.warning(
            "ingredient_link.record_failed user=%s norm=%r product=%s",
            user_id,
            norm,
            product_id,
            exc_info=True,
        )


def _load_account_links(cursor: Any, user_id: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT norm_name, raw_name, product_id, product_description,
               times_linked, last_linked_at
        FROM ingredient_links
        WHERE user_id = ?
        """,
        (user_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_canonical_name(user_id: str, raw_name: str) -> dict[str, Any] | None:
    """Suggest the account's standard surface form for ``raw_name``, or None.

    Learned, not seeded: we find which products this account has linked the
    query to (directly or via token overlap), gather every surface form the
    account has used for those same products, and return the most-frequent one.
    "fresh parsley" -> "parsley" emerges only because the account links both to
    the same product and writes "parsley" more often.

    Returns ``{"canonical_name", "confidence"}`` or None when the typed form is
    already the most-used, support is too thin, or confidence is below
    threshold (we never nag with a low-evidence rename).
    """
    norm = normalize_ingredient_name(raw_name)
    if not user_id or not norm:
        return None
    try:
        ensure_initialized()
        with get_db_cursor() as cursor:
            rows = _load_account_links(cursor, user_id)
    except Exception:
        logger.warning("ingredient_link.canonical_failed user=%s", user_id, exc_info=True)
        return None

    query_tokens = _tokens(norm)
    product_ids = {
        r["product_id"]
        for r in rows
        if r["norm_name"] == norm or _tokens(r["norm_name"]) & query_tokens
    }
    if not product_ids:
        return None

    # Tally surface forms across the matched product cluster.
    form_counts: dict[str, int] = {}
    for r in rows:
        if r["product_id"] in product_ids:
            form = (r["raw_name"] or "").strip()
            if form:
                form_counts[form] = form_counts.get(form, 0) + int(r["times_linked"] or 1)
    if not form_counts:
        return None

    total = sum(form_counts.values())
    canonical, top = max(form_counts.items(), key=lambda kv: kv[1])
    confidence = top / total if total else 0.0

    if total < _CANONICAL_MIN_SUPPORT or confidence < _CANONICAL_MIN_CONFIDENCE:
        return None
    if canonical.strip().lower() == raw_name.strip().lower():
        return None  # already standard
    return {"canonical_name": canonical, "confidence": round(confidence, 2)}


def _recency_boost(last_linked_at: str | None) -> float:
    if not last_linked_at:
        return 0.0
    try:
        delta = datetime.now() - datetime.fromisoformat(last_linked_at)
    except (ValueError, TypeError):
        return 0.0
    return _W_RECENT_LINK if delta.days <= _RECENT_DAYS else 0.0


def _purchase_signal(cursor: Any, user_id: str, product_ids: set[str]) -> dict[str, int]:
    """Map product_id -> total_purchases for this account. Best-effort."""
    if not product_ids:
        return {}
    placeholders = ",".join("?" for _ in product_ids)
    try:
        cursor.execute(
            f"""
            SELECT product_id, total_purchases
            FROM product_statistics
            WHERE user_id = ? AND product_id IN ({placeholders})
            """,
            (user_id, *product_ids),
        )
        return {row["product_id"]: int(row["total_purchases"] or 0) for row in cursor.fetchall()}
    except Exception:
        # Pre-multi-tenant DBs may lack product_statistics.user_id; degrade.
        logger.debug("purchase_signal unavailable user=%s", user_id, exc_info=True)
        return {}


def _membership(cursor: Any, table: str, user_id: str, product_ids: set[str]) -> set[str]:
    """product_ids present in a user-scoped table (pantry/favorites/blocked)."""
    if not product_ids:
        return set()
    placeholders = ",".join("?" for _ in product_ids)
    try:
        cursor.execute(
            f"SELECT product_id FROM {table} "
            f"WHERE user_id = ? AND product_id IN ({placeholders})",
            (user_id, *product_ids),
        )
        return {row["product_id"] for row in cursor.fetchall()}
    except Exception:
        logger.debug("membership unavailable table=%s user=%s", table, user_id, exc_info=True)
        return set()


def suggest_products_for_ingredient(
    user_id: str, name: str, limit: int = 6
) -> list[dict[str, Any]]:
    """Rank this account's "usual" products for ingredient ``name``.

    Blend (per the agreed signals): prior links for this ingredient (dominant),
    purchase frequency, pantry/favorites membership, and a safety demotion for
    blocked products. Recall is exact normalized match first, then token overlap
    against the account's own past ingredient names (still history-only, no
    dictionary).

    Each result: ``{product_id, product_description, score, reason,
    times_linked}``. Empty list on cold start (no history) — callers fall back
    to plain live search. Never raises; logs and returns [] on failure.
    """
    norm = normalize_ingredient_name(name)
    if not user_id or not norm:
        return []
    try:
        ensure_initialized()
        with get_db_cursor() as cursor:
            rows = _load_account_links(cursor, user_id)
            if not rows:
                return []
            candidates = _recall_candidates(rows, norm)
            if not candidates:
                return []
            product_ids = set(candidates)
            purchases = _purchase_signal(cursor, user_id, product_ids)
            in_pantry = _membership(cursor, "pantry_items", user_id, product_ids)
            in_favorites = _membership(cursor, "favorite_list_items", user_id, product_ids)
            blocked = _membership(cursor, "blocked_products", user_id, product_ids)
    except Exception:
        logger.warning("ingredient_link.suggest_failed user=%s", user_id, exc_info=True)
        return []

    scored = [
        _score_candidate(c, purchases, in_pantry, in_favorites, blocked)
        for c in candidates.values()
    ]
    scored.sort(key=lambda s: s["score"], reverse=True)
    top = scored[: max(0, limit)]
    logger.info(
        "ingredient_link.suggest user=%s norm=%r candidates=%d returned=%d",
        user_id,
        norm,
        len(candidates),
        len(top),
    )
    return top


def _recall_candidates(rows: list[dict[str, Any]], norm: str) -> dict[str, dict[str, Any]]:
    """Collapse link rows to one entry per product with a recall match score.

    Exact normalized-name match scores 1.0; otherwise Jaccard token overlap
    against the account's stored names (0..1). Products with no overlap are
    dropped.
    """
    query_tokens = _tokens(norm)
    by_product: dict[str, dict[str, Any]] = {}
    for r in rows:
        stored_tokens = _tokens(r["norm_name"])
        if r["norm_name"] == norm:
            match = 1.0
        elif query_tokens and stored_tokens:
            overlap = query_tokens & stored_tokens
            match = len(overlap) / len(query_tokens | stored_tokens) if overlap else 0.0
        else:
            match = 0.0
        if match <= 0.0:
            continue
        pid = r["product_id"]
        existing = by_product.get(pid)
        if existing is None or match > existing["match"] or (
            match == existing["match"] and (r["times_linked"] or 0) > existing["times_linked"]
        ):
            by_product[pid] = {
                "product_id": pid,
                "product_description": r["product_description"],
                "times_linked": int(r["times_linked"] or 1),
                "last_linked_at": r["last_linked_at"],
                "match": match,
            }
    return by_product


def _score_candidate(
    candidate: dict[str, Any],
    purchases: dict[str, int],
    in_pantry: set[str],
    in_favorites: set[str],
    blocked: set[str],
) -> dict[str, Any]:
    pid = candidate["product_id"]
    times = candidate["times_linked"]
    score = candidate["match"]
    score += min(times, _LINK_FREQ_CAP) * _W_LINK_FREQ
    score += _recency_boost(candidate["last_linked_at"])

    bought = purchases.get(pid, 0)
    score += min(bought, _PURCHASE_CAP) * _W_PURCHASE_FREQ

    tags: list[str] = []
    if times >= 1:
        tags.append(f"Linked {times}×")
    if bought > 0:
        tags.append(f"Bought {bought}×")
    if pid in in_pantry:
        score += _W_IN_PANTRY
        tags.append("In pantry")
    if pid in in_favorites:
        score += _W_IN_FAVORITES
        tags.append("In favorites")
    if pid in blocked:
        score *= _BLOCKED_PENALTY
        tags.append("Blocked")

    return {
        "product_id": pid,
        "product_description": candidate["product_description"],
        "times_linked": times,
        "score": round(score, 3),
        "reason": " · ".join(tags) if tags else "Previously linked",
    }


def best_guess(suggestions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one product to pre-select for one-tap linking, or None.

    Only returns a guess when the top suggestion clears a confidence floor — we
    never auto-highlight a weak match (and a blocked product, demoted below the
    floor, can never become the guess).
    """
    if not suggestions:
        return None
    top = suggestions[0]
    return top if top["score"] >= _BEST_GUESS_MIN_SCORE else None
