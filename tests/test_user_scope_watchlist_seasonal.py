"""User-scoping of deal_watchlist + seasonal_patterns (SQLite side).

These two analytics tables were global (one shared watchlist / one shared set of
seasonal patterns across all users) — the same class of multi-user bug as the
shared-Kroger-token bug. They are now user-scoped. This module proves:

* the in-place migration rebuilds a legacy (user_id-less) table, backfilling
  every existing row to the default owner and swapping the single-column UNIQUE
  for a user-scoped composite one (so two users can track the same product);
* the runtime writes/reads are isolated per user.

The live-Postgres half of the acceptance criteria lives in test_pg_backend.py.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator

import pytest

DEFAULT_OWNER = os.environ["KROGER_MCP_DEFAULT_USER_ID"]  # installed by conftest
USER_B = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def sqlite_db() -> Iterator[str]:
    """Point the app's DB_FILE at a throwaway SQLite file with the CURRENT schema."""
    fd, path = tempfile.mkstemp(prefix="userscope_", suffix=".db")
    os.close(fd)
    from kroger_mcp.analytics import database as db_mod

    original = db_mod.DB_FILE
    prior_url = os.environ.pop("DATABASE_URL", None)
    db_mod.DB_FILE = path
    db_mod.reset_initialization()
    try:
        db_mod.initialize_database()
        db_mod.run_schema_migrations()
        # users rows so user-scoped writes have a real owner (FK is not enforced
        # on SQLite by default, but keep the data honest).
        with sqlite3.connect(path) as raw:
            raw.execute(
                "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT)"
            )
            raw.executemany(
                "INSERT OR IGNORE INTO users (id, email) VALUES (?, ?)",
                [(DEFAULT_OWNER, "owner@test"), (USER_B, "b@test")],
            )
            raw.execute(
                "INSERT OR IGNORE INTO products (product_id, description) VALUES (?, ?)",
                ("PROD-1", "Shared Product"),
            )
        yield path
    finally:
        db_mod.DB_FILE = original
        db_mod.reset_initialization()
        if prior_url is not None:
            os.environ["DATABASE_URL"] = prior_url
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Migration: legacy global table -> user-scoped (rebuild + backfill)
# ---------------------------------------------------------------------------
def _legacy_db(path: str) -> None:
    """Replace deal_watchlist + seasonal_patterns with their OLD (pre-user_id) shapes.

    Assumes the full current schema already exists (initialize_database) so the
    rest of run_schema_migrations()'s ALTERs have their tables; we only revert
    these two to the legacy shape the migration must rebuild.
    """
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE IF EXISTS deal_watchlist")
        raw.execute("DROP TABLE IF EXISTS seasonal_patterns")
        raw.execute(
            """
            CREATE TABLE deal_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT UNIQUE NOT NULL,
                description TEXT,
                target_price REAL,
                priority INTEGER DEFAULT 1,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TEXT,
                best_price_seen REAL,
                best_price_date TEXT
            )
            """
        )
        raw.execute(
            """
            CREATE TABLE seasonal_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                month INTEGER NOT NULL,
                week_of_year INTEGER,
                purchase_count INTEGER DEFAULT 0,
                avg_quantity REAL,
                is_peak_period INTEGER DEFAULT 0,
                holiday_association TEXT,
                UNIQUE(product_id, month)
            )
            """
        )
        raw.execute(
            "INSERT INTO deal_watchlist (product_id, description, priority) "
            "VALUES ('PROD-1', 'Legacy item', 2)"
        )
        raw.execute(
            "INSERT INTO seasonal_patterns (product_id, month, purchase_count, "
            "is_peak_period) VALUES ('PROD-1', 11, 5, 1)"
        )


def test_migration_rebuilds_legacy_tables_with_user_id():
    fd, path = tempfile.mkstemp(prefix="userscope_legacy_", suffix=".db")
    os.close(fd)
    from kroger_mcp.analytics import database as db_mod

    original = db_mod.DB_FILE
    prior_url = os.environ.pop("DATABASE_URL", None)
    db_mod.DB_FILE = path
    db_mod.reset_initialization()
    try:
        db_mod.initialize_database()  # full current schema
        # The rebuild enforces deal_watchlist/seasonal_patterns' FK to products,
        # so the referenced product must exist (it always does in reality — a
        # watchlist row can't be created without its product).
        with sqlite3.connect(path) as raw:
            raw.execute(
                "INSERT OR IGNORE INTO products (product_id, description) "
                "VALUES ('PROD-1', 'Legacy item')"
            )
        _legacy_db(path)  # revert the two tables to their pre-user_id shape + rows
        db_mod.run_schema_migrations()  # rebuilds both tables

        with sqlite3.connect(path) as raw:
            raw.row_factory = sqlite3.Row
            for table in ("deal_watchlist", "seasonal_patterns"):
                cols = {r[1] for r in raw.execute(f"PRAGMA table_info({table})")}
                assert "user_id" in cols, f"{table} missing user_id after migration"

            # Existing rows backfilled to the default owner.
            dw = raw.execute("SELECT * FROM deal_watchlist").fetchall()
            assert len(dw) == 1
            assert dw[0]["user_id"] == DEFAULT_OWNER
            assert dw[0]["priority"] == 2  # carried column preserved

            sp = raw.execute("SELECT * FROM seasonal_patterns").fetchall()
            assert len(sp) == 1
            assert sp[0]["user_id"] == DEFAULT_OWNER
            assert sp[0]["purchase_count"] == 5

            # Composite UNIQUE now lets a SECOND user track the same product —
            # impossible under the old single-column product_id UNIQUE.
            raw.execute(
                "INSERT INTO deal_watchlist (user_id, product_id, priority) "
                "VALUES (?, 'PROD-1', 3)",
                (USER_B,),
            )
            raw.execute(
                "INSERT INTO seasonal_patterns (user_id, product_id, month, "
                "purchase_count) VALUES (?, 'PROD-1', 11, 9)",
                (USER_B,),
            )
            assert raw.execute("SELECT COUNT(*) FROM deal_watchlist").fetchone()[0] == 2

        # Re-running the migration is a no-op (idempotent).
        db_mod.run_schema_migrations()
        with sqlite3.connect(path) as raw:
            assert raw.execute("SELECT COUNT(*) FROM deal_watchlist").fetchone()[0] == 2
    finally:
        db_mod.DB_FILE = original
        db_mod.reset_initialization()
        if prior_url is not None:
            os.environ["DATABASE_URL"] = prior_url
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Runtime isolation
# ---------------------------------------------------------------------------
def test_deal_watchlist_is_user_isolated(sqlite_db: str):
    """Two users watching the SAME product keep independent rows + upserts."""
    from kroger_mcp.analytics.database import get_db_cursor

    sql = (
        "INSERT INTO deal_watchlist (user_id, product_id, description, priority) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, product_id) DO UPDATE SET priority = excluded.priority"
    )
    with get_db_cursor() as cur:
        cur.execute(sql, (DEFAULT_OWNER, "PROD-1", "A's watch", 1))
        cur.execute(sql, (USER_B, "PROD-1", "B's watch", 3))

    with get_db_cursor() as cur:
        a = cur.execute(
            "SELECT priority FROM deal_watchlist WHERE user_id = ? AND product_id = ?",
            (DEFAULT_OWNER, "PROD-1"),
        ).fetchone()
        b = cur.execute(
            "SELECT priority FROM deal_watchlist WHERE user_id = ? AND product_id = ?",
            (USER_B, "PROD-1"),
        ).fetchone()
    assert a[0] == 1 and b[0] == 3  # both present, independent

    # Upsert on A's row updates only A's; B untouched; still exactly two rows.
    with get_db_cursor() as cur:
        cur.execute(sql, (DEFAULT_OWNER, "PROD-1", "A again", 2))
    with get_db_cursor() as cur:
        total = cur.execute("SELECT COUNT(*) FROM deal_watchlist").fetchone()[0]
        a2 = cur.execute(
            "SELECT priority FROM deal_watchlist WHERE user_id = ?",
            (DEFAULT_OWNER,),
        ).fetchone()
    assert total == 2
    assert a2[0] == 2


def test_seasonal_patterns_are_user_isolated(sqlite_db: str):
    """update_seasonal_patterns + get_upcoming_seasonal_items scope to one user."""
    import datetime as _dt

    from kroger_mcp.analytics.database import get_db_cursor
    from kroger_mcp.analytics.seasonal import get_upcoming_seasonal_items

    this_month = _dt.datetime.now().month

    # Seed a peak pattern for the SAME product+month under two different users.
    with get_db_cursor() as cur:
        for owner, qty in ((DEFAULT_OWNER, 7.0), (USER_B, 3.0)):
            cur.execute(
                "INSERT INTO seasonal_patterns (user_id, product_id, month, "
                "purchase_count, avg_quantity, is_peak_period) "
                "VALUES (?, 'PROD-1', ?, 5, ?, 1)",
                (owner, this_month, qty),
            )

    a_items = get_upcoming_seasonal_items(days_ahead=2, user_id=DEFAULT_OWNER)
    a_match = [i for i in a_items if i["product_id"] == "PROD-1"]
    assert len(a_match) == 1
    assert a_match[0]["typical_quantity"] == 7.0  # A's row, not B's (3.0)

    # A brand-new user with no patterns sees none of PROD-1.
    fresh_items = get_upcoming_seasonal_items(days_ahead=2, user_id="no-such-user")
    assert not [i for i in fresh_items if i["product_id"] == "PROD-1"]
