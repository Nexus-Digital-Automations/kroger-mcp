"""Favorite-on-sale alerts: daily detection + per-user read/state.

Ownership: backs the in-app notification bell. ``scan_favorites_for_sales`` runs
from the launchd background scanner (outside any request); the read/state
helpers are called by ``web/routes/api/notifications.py``.

All SQL is portable across the SQLite/Postgres backends via the database
adapter: ``?`` placeholders, the ``INSERT OR IGNORE`` idiom (rewritten to
``ON CONFLICT DO NOTHING`` on PG), and integer ``0|1`` flag columns.

Trigger rule: a favorite is alerted only when it is on sale now AND was NOT on
sale at the previous price observation ("newly on sale") — one alert per sale
event, deduped further by the ``UNIQUE(user_id, product_id, sale_price)`` key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .database import ensure_initialized, get_db_connection, get_db_cursor
from .deals import record_price_observation

logger = logging.getLogger(__name__)

DEFAULT_LOCATION_ID = "03400014"

# (product_id, location_id) -> extracted product dict (on_sale/sale_price/...) or None
PriceLookup = Callable[[str, str], "dict | None"]

_ALERT_COLUMNS = (
    "id, product_id, list_id, description, brand, regular_price, sale_price, "
    "savings_percent, default_quantity, preferred_modality, created_at, "
    "seen, dismissed, acted"
)


# ── read / state (per user; called by the API) ──────────────────────────────
def _to_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _row_to_alert(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "list_id": row["list_id"],
        "description": row["description"],
        "brand": row["brand"],
        "regular_price": _to_float(row["regular_price"]),
        "sale_price": _to_float(row["sale_price"]),
        "savings_percent": _to_float(row["savings_percent"]),
        "default_quantity": _to_float(row["default_quantity"]),
        "preferred_modality": row["preferred_modality"],
        "created_at": row["created_at"],
        "seen": bool(row["seen"]),
        "dismissed": bool(row["dismissed"]),
        "acted": bool(row["acted"]),
    }


def list_alerts(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Active (non-dismissed) sale alerts for a user, newest first."""
    ensure_initialized()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            f"SELECT {_ALERT_COLUMNS} FROM favorite_sale_alerts "
            "WHERE user_id = ? AND dismissed = 0 "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        )
        return [_row_to_alert(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def unseen_count(user_id: str) -> int:
    """Count of active, not-yet-seen alerts — drives the bell badge."""
    ensure_initialized()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) AS n FROM favorite_sale_alerts "
            "WHERE user_id = ? AND dismissed = 0 AND seen = 0",
            (user_id,),
        )
        row = cursor.fetchone()
        return int(row["n"]) if row is not None else 0
    finally:
        conn.close()


def list_pending_meals_for_bell(user_id: str) -> list[dict[str, Any]]:
    """Past, unconfirmed meal-plan entries for the notification bell's
    "pending meals" section. Thin wrapper over meal_planning.list_pending_meals
    that adds a stable composite id the bell UI can key rows on.

    Safe to call regardless of meal_plan_pantry_deduction_mode: in 'automatic'
    mode these are usually already cleared by the lazy reconciler, but if a
    user hasn't triggered that yet, surfacing them here is harmless.
    """
    from .meal_planning import list_pending_meals

    meals = list_pending_meals(user_id=user_id)
    for meal in meals:
        meal["id"] = f"{meal['plan_id']}|{meal['meal_date']}|{meal['meal_slot']}"
    return meals


def list_pantry_alerts_for_bell(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """Low-stock / expiring-within-7-days pantry items for the bell.

    Reuses pantry.get_pantry_status (favorites-cadence depletion, expiration
    recalculation) rather than duplicating its predicate, so the bell and the
    dashboard's "Pantry Needs Attention" card never disagree on what counts
    as an alert.
    """
    from .pantry import get_pantry_status

    items = get_pantry_status(user_id=user_id)
    alerts = [
        item
        for item in items
        if item["status"] in ("low", "out")
        or (item["days_to_expiration"] is not None and item["days_to_expiration"] <= 7)
    ]
    alerts.sort(key=lambda item: item["level_percent"])
    return alerts[:limit]


def list_unmatched_snacks_for_bell(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """Recent snack-log entries that matched no pantry item (14-day window).

    Decision: unmatched snacks never error the log call — they're recorded
    silently and surfaced here (bell + pantry get_attention) so the user can
    fix them on their own schedule. Newest first; no dismiss flow — entries
    age out of the window naturally.
    """
    from datetime import timedelta

    ensure_initialized()
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT id, item_text, logged_at FROM unmatched_snack_log "
            "WHERE user_id = ? AND logged_at >= ? "
            "ORDER BY logged_at DESC, id DESC LIMIT ?",
            (user_id, cutoff, limit),
        )
        return [
            {"id": row["id"], "item_text": row["item_text"], "logged_at": row["logged_at"]}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def next_week_needs_plan(user_id: str) -> bool:
    """True if no meal plan covers next week's first day -- a bell reminder.

    Checking a single anchor date (next week's start, per the user's
    configured week_start_day setting) rather than the whole week is enough:
    weekly plans are created aligned to that same boundary, so any plan
    covering the first day covers the upcoming week. Draft plans don't count
    (find_plan_covering_date excludes them), so the reminder stays up until
    the auto-drafted week is actually approved.
    """
    from datetime import timedelta

    from kroger_mcp.tools.shared import get_week_start_day

    from .meal_planning import find_plan_covering_date, week_start_for_date

    next_start = week_start_for_date(
        datetime.now().date(), get_week_start_day(user_id=user_id)
    ) + timedelta(days=7)
    return find_plan_covering_date(next_start.isoformat(), user_id=user_id) is None


def mark_alerts_seen(user_id: str) -> int:
    """Clear the badge: mark all of a user's active alerts as seen."""
    ensure_initialized()
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE favorite_sale_alerts SET seen = 1 "
            "WHERE user_id = ? AND seen = 0 AND dismissed = 0",
            (user_id,),
        )
        return cursor.rowcount or 0


def dismiss_alert(user_id: str, alert_id: int) -> bool:
    """Remove a single alert from the user's list. Returns True if one changed."""
    ensure_initialized()
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE favorite_sale_alerts SET dismissed = 1 "
            "WHERE id = ? AND user_id = ?",
            (alert_id, user_id),
        )
        return (cursor.rowcount or 0) > 0


def mark_acted(user_id: str, alert_id: int) -> bool:
    """Mark an alert acted-on (added to list/cart) and dismiss it."""
    ensure_initialized()
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE favorite_sale_alerts SET acted = 1, dismissed = 1 "
            "WHERE id = ? AND user_id = ?",
            (alert_id, user_id),
        )
        return (cursor.rowcount or 0) > 0


# ── detection (background scan) ─────────────────────────────────────────────
def _favorited_products() -> dict[str, list[dict[str, Any]]]:
    """Map product_id -> list of per-user favorite rows (one product, many owners)."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT fli.product_id, fli.description, fli.brand, "
            "fli.default_quantity, fli.preferred_modality, "
            "fli.list_id, fl.user_id "
            "FROM favorite_list_items fli "
            "JOIN favorite_lists fl ON fl.id = fli.list_id"
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in cursor.fetchall():
            grouped.setdefault(row["product_id"], []).append(
                {
                    "user_id": row["user_id"],
                    "list_id": row["list_id"],
                    "description": row["description"],
                    "brand": row["brand"],
                    "default_quantity": row["default_quantity"],
                    "preferred_modality": row["preferred_modality"],
                }
            )
        return grouped
    finally:
        conn.close()


def _previous_on_sale(product_id: str, location_id: str) -> bool:
    """The on_sale state at the most recent prior price observation (False if none)."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT on_sale FROM price_history "
            "WHERE product_id = ? AND location_id = ? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1",
            (product_id, location_id),
        )
        row = cursor.fetchone()
        return bool(row["on_sale"]) if row is not None else False
    finally:
        conn.close()


def _default_price_lookup(product_id: str, location_id: str) -> dict[str, Any] | None:
    """Fetch a product's current price via the app-level Kroger client."""
    from kroger_mcp.auth.dependencies import mcp_user_id
    from kroger_mcp.tools.shared import get_client_credentials_client
    from kroger_mcp.web.routes.api._product_extract import _extract_product

    client = get_client_credentials_client(mcp_user_id())
    raw = client.product.get_product(product_id=product_id, location_id=location_id)
    record = raw.get("data") if isinstance(raw, dict) else getattr(raw, "data", None)
    return _extract_product(record) if record else None


def _create_alerts_for_product(
    owners: list[dict[str, Any]], extracted: dict[str, Any], now: str
) -> int:
    """Insert one alert per owner for a newly-on-sale product. Dedup via unique key."""
    created = 0
    with get_db_cursor() as cursor:
        for owner in owners:
            cursor.execute(
                "INSERT OR IGNORE INTO favorite_sale_alerts "
                "(user_id, product_id, list_id, description, brand, "
                " regular_price, sale_price, savings_percent, "
                " default_quantity, preferred_modality, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    owner["user_id"],
                    extracted["product_id"],
                    owner["list_id"],
                    owner["description"],
                    owner["brand"],
                    extracted.get("regular_price"),
                    extracted.get("sale_price"),
                    extracted.get("savings_percent") or 0,
                    owner["default_quantity"] or 1,
                    owner["preferred_modality"] or "PICKUP",
                    now,
                ),
            )
            created += cursor.rowcount or 0
    return created


def _dismiss_stale_alerts(product_id: str) -> None:
    """Auto-dismiss outstanding alerts once a product is no longer on sale."""
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE favorite_sale_alerts SET dismissed = 1 "
            "WHERE product_id = ? AND dismissed = 0",
            (product_id,),
        )


def scan_favorites_for_sales(
    location_id: str = DEFAULT_LOCATION_ID,
    price_lookup: PriceLookup | None = None,
) -> int:
    """Check every favorited product's price; alert owners on newly-on-sale items.

    Returns the number of alerts created. Per-product failures are logged and
    skipped so one bad product never aborts the whole scan. ``price_lookup`` is
    injectable for testing; it defaults to a live client-credentials fetch.
    """
    ensure_initialized()
    lookup = price_lookup or _default_price_lookup
    grouped = _favorited_products()
    logger.info("favorites_sale_scan_start products=%d location=%s", len(grouped), location_id)

    created_total = 0
    for product_id, owners in grouped.items():
        try:
            was_on_sale = _previous_on_sale(product_id, location_id)
            extracted = lookup(product_id, location_id)
            if not extracted:
                continue

            on_sale = bool(extracted.get("on_sale"))
            # Record the fresh observation (after reading the prior state).
            record_price_observation(
                product_id,
                extracted.get("regular_price"),
                extracted.get("sale_price"),
                location_id,
                source="favorites_scan",
            )

            if on_sale and not was_on_sale:
                extracted.setdefault("product_id", product_id)
                created_total += _create_alerts_for_product(
                    owners, extracted, datetime.now().isoformat()
                )
            elif not on_sale:
                _dismiss_stale_alerts(product_id)
        except Exception:
            logger.warning(
                "favorites_sale_scan product failed product_id=%s", product_id, exc_info=True
            )

    logger.info("favorites_sale_scan_done alerts_created=%d", created_total)
    return created_total
