#!/usr/bin/env python3
"""Add the performance indexes to an EXISTING production Postgres database.

Fresh installs already get these from ``pg_database.SCHEMA_SQL``; this script
backfills a live DB that predates them. Every index is built with
``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` so it:

  - does NOT take a long lock — reads and writes continue during the build, and
  - is idempotent — re-running skips indexes that already exist.

``CONCURRENTLY`` cannot run inside a transaction block, so the connection runs in
autocommit mode. Good-neighbor note: this only touches the ``smartshopper``
database named in ``DATABASE_URL``; no other tenant on the box is affected.

Usage (on the prod box, with DATABASE_URL set in the environment / .env):
    uv run --frozen python scripts/add_perf_indexes.py
"""

from __future__ import annotations

import os
import sys

# Indexes mirror the additions in pg_database.SCHEMA_SQL. Name → DDL (sans the
# CREATE INDEX CONCURRENTLY IF NOT EXISTS prefix, added below).
INDEXES: dict[str, str] = {
    "idx_pe_event_date": "purchase_events (event_date DESC)",
    "idx_pe_product_event_type": "purchase_events (product_id, event_type)",
    "idx_pe_order_id": "purchase_events (order_id)",
    "idx_orders_placed_at": "orders (placed_at DESC)",
    "idx_price_location_date": "price_history (location_id, observed_at DESC)",
}


def main() -> int:
    # Load the project .env (DATABASE_URL) when run standalone on the prod box.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set — this script is Postgres-only. Nothing to do.")
        return 0

    import psycopg

    print(f"Connecting to Postgres ({_redact(database_url)}) …")
    # autocommit is REQUIRED: CREATE INDEX CONCURRENTLY forbids a transaction.
    with psycopg.connect(database_url, autocommit=True) as conn:
        for name, target in INDEXES.items():
            ddl = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {target}"
            try:
                conn.execute(ddl)
                print(f"  ✓ {name}  ON {target}")
            except Exception as exc:  # noqa: BLE001 — report and continue per index
                print(f"  ✗ {name}: {exc}", file=sys.stderr)
                return 1
    print("Done. All performance indexes present.")
    return 0


def _redact(url: str) -> str:
    """Hide any password in the URL before printing."""
    if "@" not in url:
        return url
    creds, host = url.rsplit("@", 1)
    scheme_user = creds.split(":")[0:2]
    return f"{':'.join(scheme_user)}:***@{host}"


if __name__ == "__main__":
    raise SystemExit(main())
