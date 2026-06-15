"""Test that cart mark_placed restocks pantry items via the web route."""
import asyncio
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

from _pg_support import skip_on_pg

# This test harness opens sqlite3.connect(DB_FILE) directly to set up/verify and
# uses a non-UUID "test-user" — both SQLite-only. The restock data path itself
# (restock_item via get_db_cursor) is exercised on Postgres by test_pg_backend's
# pantry write+read, so skipping the web-route integration here loses no PG coverage.
pytestmark = skip_on_pg


def _fake_request(user_id: str = "test-user"):
    """Minimal stand-in for FastAPI Request carrying an authenticated user.

    mark_order_placed now resolves the per-user Kroger client via
    current_user_id(request); the route is invoked directly here (not through
    the app), so we supply the request.state.user the middleware would set.
    """
    return SimpleNamespace(state=SimpleNamespace(user={"id": user_id}))


def _purge_test_orders_from_history(test_product_ids: list, order_history_file: str) -> None:
    """Remove orders containing test product IDs from kroger_order_history.json."""
    if not os.path.exists(order_history_file):
        return
    with open(order_history_file, 'r') as f:
        history = json.load(f)
    test_ids = set(test_product_ids)
    cleaned = [
        order for order in history
        if not any(
            item.get('product_id') in test_ids
            for item in order.get('items', [])
        )
    ]
    with open(order_history_file, 'w') as f:
        json.dump(cleaned, f)


def _clear_kroger_modules():
    for k in list(sys.modules.keys()):
        if 'kroger_mcp' in k:
            del sys.modules[k]


class TestMarkPlacedRestockWebRoute:
    """Tests for the pantry restock logic added to mark_order_placed."""

    def test_happy_path_restocks_pantry_in_real_db(self):
        """mark_order_placed writes cart items to real SQLite pantry at level 100."""
        _clear_kroger_modules()
        import importlib

        cr = importlib.import_module('kroger_mcp.web.routes.api.cart')
        import kroger_mcp.analytics.database as db_mod
        import kroger_mcp.analytics.pantry as pantry_mod
        import kroger_mcp.tools.cart_tools as ct

        product_id = 'PYTEST_RESTOCK_HAPPY_001'
        backup = ct._load_cart_data()

        try:
            ct._save_cart_data(
                {'current_cart': [{'product_id': product_id, 'name': 'TestItem', 'quantity': 1}]},
            )
            pantry_mod.ensure_initialized()
            conn = sqlite3.connect(db_mod.DB_FILE)
            before = conn.execute(
                'SELECT level_percent FROM pantry_items WHERE product_id=?', (product_id,)
            ).fetchone()
            conn.close()

            cr.record_order = lambda *a, **kw: 'test-ord'
            resp = asyncio.run(cr.mark_order_placed(_fake_request()))

            conn = sqlite3.connect(db_mod.DB_FILE)
            after = conn.execute(
                'SELECT level_percent FROM pantry_items WHERE product_id=?', (product_id,)
            ).fetchone()
            conn.close()

            print(f"\n  Pantry BEFORE mark_placed: {before}")
            print(f"  HTTP response: {resp.status_code} {json.loads(resp.body)}")
            print(f"  Pantry AFTER mark_placed:  {after}")
            assert resp.status_code == 200
            body = json.loads(resp.body)
            assert body['success'] is True
            assert before is None, f"Item should not exist before: {before}"
            assert after is not None, "Item should exist in pantry after mark_placed"
            assert after[0] == 100, f"Level should be 100, got {after[0]}"
            print(f"  PASS: pantry level = {after[0]}%")

        finally:
            conn = sqlite3.connect(db_mod.DB_FILE)
            conn.execute('DELETE FROM pantry_items WHERE product_id=?', (product_id,))
            conn.execute('DELETE FROM products WHERE product_id=?', (product_id,))
            conn.commit()
            conn.close()
            ct._save_cart_data(backup)
            _purge_test_orders_from_history([product_id], ct.ORDER_HISTORY_FILE)

    def test_item_without_product_id_skipped_silently(self):
        """Items missing product_id are skipped; items with product_id are restocked."""
        _clear_kroger_modules()
        import importlib

        cr = importlib.import_module('kroger_mcp.web.routes.api.cart')
        import kroger_mcp.analytics.database as db_mod
        import kroger_mcp.analytics.pantry as pantry_mod
        import kroger_mcp.tools.cart_tools as ct

        product_id = 'PYTEST_RESTOCK_ERR_002'
        backup = ct._load_cart_data()

        try:
            ct._save_cart_data(
                {'current_cart': [
                    {'name': 'NoIDItem', 'quantity': 1},
                    {'product_id': product_id, 'name': 'RealItem', 'quantity': 1},
                ]},
            )
            pantry_mod.ensure_initialized()
            cr.record_order = lambda *a, **kw: 'test-ord'
            resp = asyncio.run(cr.mark_order_placed(_fake_request()))

            conn = sqlite3.connect(db_mod.DB_FILE)
            after = conn.execute(
                'SELECT level_percent FROM pantry_items WHERE product_id=?', (product_id,)
            ).fetchone()
            conn.close()

            print("\n  Cart had 2 items: 1 without product_id (skipped), 1 with id")
            print(f"  HTTP response: {resp.status_code}")
            print(f"  Pantry AFTER for real item: {after}")
            assert resp.status_code == 200
            assert after is not None, "Item with product_id should be in pantry"
            assert after[0] == 100
            print(f"  PASS: real item restocked at {after[0]}%, no-id item silently skipped")

        finally:
            conn = sqlite3.connect(db_mod.DB_FILE)
            conn.execute('DELETE FROM pantry_items WHERE product_id=?', (product_id,))
            conn.execute('DELETE FROM products WHERE product_id=?', (product_id,))
            conn.commit()
            conn.close()
            ct._save_cart_data(backup)
            _purge_test_orders_from_history([product_id], ct.ORDER_HISTORY_FILE)
