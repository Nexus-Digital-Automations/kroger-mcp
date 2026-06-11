"""Seed the LOCAL dev database with synthetic Smart Shopper data.

For the development box (the MacBook Air) only — NEVER real PII. Generates fake
users (@example.com), products, pantry levels, and price history so the app is
usable in dev without touching the production data on the mini.

Safety:
- Refuses to run when APP_ENV=prod (the production guard).
- Idempotent: re-running tops up missing rows without duplicating.
- Deterministic (fixed RNG seed) so dev state is reproducible.

Run:  APP_ENV=dev python scripts/seed_dummy.py
Respects DATABASE_URL (Postgres) / falls back to the SQLite dev file.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

# Deterministic dev data.
random.seed(1337)

_DUMMY_USERS = [
    ("ada@example.com", "Ada (dev)", "devpassword1"),
    ("grace@example.com", "Grace (dev)", "devpassword2"),
    ("linus@example.com", "Linus (dev)", "devpassword3"),
]

_DUMMY_PRODUCTS = [
    ("DUMMY-0001", "Organic Whole Milk, 1 gal", "Simple Truth"),
    ("DUMMY-0002", "Extra Virgin Olive Oil, 500ml", "Private Selection"),
    ("DUMMY-0003", "Heirloom Tomatoes, lb", "Fresh"),
    ("DUMMY-0004", "Boneless Skinless Chicken Breast, lb", "Kroger"),
    ("DUMMY-0005", "Brown Rice, 2 lb", "Simple Truth"),
    ("DUMMY-0006", "Wild Caught Salmon Fillet, lb", "Private Selection"),
    ("DUMMY-0007", "Baby Spinach, 5 oz", "Fresh"),
    ("DUMMY-0008", "Greek Yogurt, plain, 32 oz", "Simple Truth"),
    ("DUMMY-0009", "Black Beans, 15 oz can", "Kroger"),
    ("DUMMY-0010", "Sourdough Bread, loaf", "Private Selection"),
]

_LOCATION_ID = "03400014"


def _refuse_in_prod() -> None:
    if os.environ.get("APP_ENV") == "prod":
        sys.exit("Refusing to seed dummy data: APP_ENV=prod. This is a dev-only script.")


def _backend_and_conn():
    from kroger_mcp.analytics.database import get_backend

    if get_backend() == "postgresql":
        from kroger_mcp.analytics.pg_database import get_pg_connection

        return "postgresql", get_pg_connection()
    from kroger_mcp.analytics.database import get_db_connection

    return "sqlite", get_db_connection()


def _seed_users() -> list[str]:
    """Create dummy users via the real registration helper (idempotent)."""
    from kroger_mcp.web.routes.auth import _create_user, _email_exists, _get_user_by_email

    ids: list[str] = []
    for email, display_name, password in _DUMMY_USERS:
        if _email_exists(email):
            existing = _get_user_by_email(email)
            if existing:
                ids.append(str(existing["id"]))
            continue
        ids.append(_create_user(email, display_name, password))
    return ids


def _seed_catalog_and_pantry() -> None:
    backend, conn = _backend_and_conn()
    ph = "%s" if backend == "postgresql" else "?"
    now = datetime.now(timezone.utc)
    try:
        # price_history has no unique key (it's a trail) — clear prior seed rows
        # so re-runs stay idempotent.
        conn.execute(f"DELETE FROM price_history WHERE source = {ph}", ("seed_dummy",))
        for i, (pid, desc, brand) in enumerate(_DUMMY_PRODUCTS):
            conn.execute(
                f"INSERT INTO products (product_id, description, brand) "
                f"VALUES ({ph}, {ph}, {ph}) ON CONFLICT (product_id) DO NOTHING",
                (pid, desc, brand),
            )
            # Pantry level varies so the dashboard shows a mix of low/full items.
            level = (i * 17 + 10) % 100
            conn.execute(
                f"INSERT INTO pantry_items (product_id, description, level_percent, last_restocked_at) "
                f"VALUES ({ph}, {ph}, {ph}, {ph}) ON CONFLICT (product_id) DO NOTHING",
                (pid, desc, level, now.isoformat()),
            )
            # A short price-history trail per product (some on sale).
            for d in range(6):
                observed = (now - timedelta(days=d * 5)).isoformat()
                regular = round(2.49 + i * 0.7, 2)
                on_sale = 1 if (i + d) % 3 == 0 else 0
                sale = round(regular * 0.8, 2) if on_sale else regular
                conn.execute(
                    f"INSERT INTO price_history "
                    f"(product_id, regular_price, sale_price, on_sale, savings_amount, "
                    f"location_id, observed_at, source) "
                    f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
                    (pid, regular, sale, on_sale, round(regular - sale, 2), _LOCATION_ID, observed, "seed_dummy"),
                )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    _refuse_in_prod()

    # Ensure schema exists for whichever backend is active.
    from kroger_mcp.analytics.database import ensure_initialized
    from kroger_mcp.analytics.pg_database import initialize_sqlite_auth_tables

    ensure_initialized()
    from kroger_mcp.analytics.database import get_backend

    if get_backend() == "sqlite":
        initialize_sqlite_auth_tables()

    user_ids = _seed_users()
    _seed_catalog_and_pantry()
    print(
        f"Seeded dummy data: {len(user_ids)} users, {len(_DUMMY_PRODUCTS)} products "
        f"+ pantry + price history (backend={get_backend()}). All fake — @example.com only."
    )


if __name__ == "__main__":
    main()
