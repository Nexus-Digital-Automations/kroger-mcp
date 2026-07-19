"""Tests for favorite-on-sale alert detection + per-user read/state.

Data-integrity critical path: the scan must alert exactly once per *newly* on
sale favorite (no spam), dedupe re-runs, auto-dismiss when a sale ends, and
scope reads/state to the owning user.
"""

import importlib
import os
import sys

import pytest

from kroger_mcp.analytics import notifications
from kroger_mcp.analytics.database import get_db_cursor

USER = os.environ["KROGER_MCP_DEFAULT_USER_ID"]
LIST_ID = "TESTLIST_fav_alerts"
PID = "TEST_FAV_SALE_1"
LOC = "03400014"


def _on_sale_lookup(product_id, location_id):
    """Fake price lookup: only the TEST product is on sale; others → None
    (so real favorites in a shared dev DB are left untouched)."""
    if product_id != PID:
        return None
    return {
        "product_id": PID,
        "regular_price": 5.00,
        "sale_price": 3.50,
        "on_sale": True,
        "savings_percent": 30.0,
    }


def _not_on_sale_lookup(product_id, location_id):
    if product_id != PID:
        return None
    return {
        "product_id": PID,
        "regular_price": 5.00,
        "sale_price": None,
        "on_sale": False,
        "savings_percent": 0.0,
    }


def _seed_favorite():
    with get_db_cursor() as cursor:
        # price_history.product_id has an FK to products(product_id). Previously
        # unenforced in practice because this ran against the shared dev DB,
        # where a prior run's PID row usually already existed — a properly
        # isolated DB has neither, so it must be seeded explicitly.
        cursor.execute(
            "INSERT OR IGNORE INTO products (product_id, description) VALUES (?, ?)",
            (PID, "Test Favorite Beans"),
        )
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_lists (id, name, user_id) VALUES (?, ?, ?)",
            (LIST_ID, "Test Alerts List", USER),
        )
        cursor.execute(
            "INSERT OR IGNORE INTO favorite_list_items "
            "(list_id, product_id, description, brand, default_quantity, preferred_modality) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (LIST_ID, PID, "Test Favorite Beans", "TestBrand", 2, "DELIVERY"),
        )


def _cleanup():
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM favorite_sale_alerts WHERE product_id LIKE 'TEST%'")
        cursor.execute("DELETE FROM favorite_list_items WHERE list_id = ?", (LIST_ID,))
        cursor.execute("DELETE FROM favorite_lists WHERE id = ?", (LIST_ID,))
        cursor.execute("DELETE FROM price_history WHERE product_id LIKE 'TEST%'")


@pytest.fixture(scope="function")
def clean_db(tmp_path, monkeypatch):
    """Rebind to the live modules, then point them at an isolated DB.

    ``test_cart_mark_placed_restock`` deletes every ``kroger_mcp`` module from
    ``sys.modules`` mid-suite. That orphans this file's import-time
    ``notifications`` / ``get_db_cursor`` references against a stale ``database``
    module, so the scan writes through one module's connection while the reads
    go through another — a cross-file flake. Re-import ``database`` from the live
    ``sys.modules``, reload ``notifications`` so its ``from .database import``
    rebinds to that live module, and rebind this module's globals so seeding,
    scanning, and listing all run through one, consistent module set.

    That module set previously pointed at the real default DB_FILE (this
    fixture's own DELETEs are prefix/id-scoped, but still touched whatever DB
    happened to be configured) — isolated to a tmp_path file like every other
    clean_db fixture, independent of the module-rebinding concern above.
    """
    db = importlib.import_module("kroger_mcp.analytics.database")
    notif = importlib.import_module("kroger_mcp.analytics.notifications")

    this = sys.modules[__name__]
    monkeypatch.setattr(this, "notifications", notif)
    monkeypatch.setattr(this, "get_db_cursor", db.get_db_cursor)
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "favorite_sale_alerts_test.db"))

    db.reset_initialization()
    db.ensure_initialized()
    _cleanup()
    _seed_favorite()
    yield
    _cleanup()
    db.reset_initialization()


def test_newly_on_sale_creates_one_alert(clean_db):
    created = notifications.scan_favorites_for_sales(LOC, price_lookup=_on_sale_lookup)
    assert created == 1

    alerts = notifications.list_alerts(USER)
    mine = [a for a in alerts if a["product_id"] == PID]
    assert len(mine) == 1
    alert = mine[0]
    assert alert["sale_price"] == 3.50
    assert alert["regular_price"] == 5.00
    assert alert["default_quantity"] == 2
    assert alert["preferred_modality"] == "DELIVERY"
    assert alert["list_id"] == LIST_ID
    assert notifications.unseen_count(USER) >= 1


def test_rerun_does_not_duplicate(clean_db):
    first = notifications.scan_favorites_for_sales(LOC, price_lookup=_on_sale_lookup)
    second = notifications.scan_favorites_for_sales(LOC, price_lookup=_on_sale_lookup)
    assert first == 1
    # Second run: already on sale at the prior observation → not "newly" on sale,
    # and the unique key would dedupe anyway.
    assert second == 0
    mine = [a for a in notifications.list_alerts(USER) if a["product_id"] == PID]
    assert len(mine) == 1


def test_already_on_sale_is_not_newly_on_sale(clean_db):
    # Pre-existing observation says it was already on sale → no alert.
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO price_history "
            "(product_id, regular_price, sale_price, on_sale, savings_amount, "
            " savings_percent, location_id, observed_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (PID, 5.00, 3.50, 1, 1.50, 30.0, LOC, "2026-01-01T00:00:00", "test"),
        )
    created = notifications.scan_favorites_for_sales(LOC, price_lookup=_on_sale_lookup)
    assert created == 0


def test_sale_ended_auto_dismisses(clean_db):
    notifications.scan_favorites_for_sales(LOC, price_lookup=_on_sale_lookup)
    assert any(a["product_id"] == PID for a in notifications.list_alerts(USER))

    # Next scan finds it no longer on sale → outstanding alert auto-dismissed.
    notifications.scan_favorites_for_sales(LOC, price_lookup=_not_on_sale_lookup)
    assert not any(a["product_id"] == PID for a in notifications.list_alerts(USER))


def test_mark_seen_clears_badge(clean_db):
    notifications.scan_favorites_for_sales(LOC, price_lookup=_on_sale_lookup)
    assert notifications.unseen_count(USER) >= 1
    notifications.mark_alerts_seen(USER)
    assert notifications.unseen_count(USER) == 0
    # Marked seen, but still listed until dismissed.
    assert any(a["product_id"] == PID for a in notifications.list_alerts(USER))


def test_dismiss_removes_alert(clean_db):
    notifications.scan_favorites_for_sales(LOC, price_lookup=_on_sale_lookup)
    mine = [a for a in notifications.list_alerts(USER) if a["product_id"] == PID]
    assert mine
    assert notifications.dismiss_alert(USER, mine[0]["id"]) is True
    assert not any(a["product_id"] == PID for a in notifications.list_alerts(USER))


def test_dismiss_is_user_scoped(clean_db):
    notifications.scan_favorites_for_sales(LOC, price_lookup=_on_sale_lookup)
    mine = [a for a in notifications.list_alerts(USER) if a["product_id"] == PID]
    assert mine
    other_user = "00000000-0000-0000-0000-000000000000"
    assert notifications.dismiss_alert(other_user, mine[0]["id"]) is False
    assert any(a["product_id"] == PID for a in notifications.list_alerts(USER))
