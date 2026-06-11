"""Hermetic test for scripts/etl_sqlite_to_pg.py.

Builds a temp SQLite source (real app schema + a known fixture set across an FK
chain), a throwaway local Postgres DB, runs the ETL, and asserts:
  * per-table parity (source count == target count) for every seeded table
  * a known row survives field-by-field with correct PG types
    (bool is bool, timestamptz parses, numeric is numeric, uuid is uuid)
  * idempotency: a second run leaves counts unchanged

If a local Postgres is not reachable the whole module is skipped (pytest.skip)
rather than failed. Everything (temp SQLite + test PG DB) is torn down even on
failure.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

# scripts/ has no __init__.py and an unrelated `scripts` package is installed in
# site-packages, so import the ETL by file path rather than by module name.
_ETL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "etl_sqlite_to_pg.py"

# psycopg is a hard project dependency (the PG backend uses it).
psycopg = pytest.importorskip("psycopg")

PG_ADMIN_DSN = os.environ.get("ETL_TEST_PG_ADMIN", "postgresql://localhost:5432/postgres")


# ---------------------------------------------------------------------------
# Postgres availability + throwaway DB lifecycle
# ---------------------------------------------------------------------------
def _pg_reachable() -> bool:
    """Return True if the local Postgres admin DSN accepts a connection."""
    try:
        conn = psycopg.connect(PG_ADMIN_DSN, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason=f"local Postgres not reachable at {PG_ADMIN_DSN}",
)


@pytest.fixture
def pg_db() -> Iterator[str]:
    """Create a throwaway PG database; yield its DSN; drop it in teardown."""
    db_name = f"etl_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(PG_ADMIN_DSN, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        admin.close()

    # Build a DSN to the new DB by swapping the trailing /dbname.
    base = PG_ADMIN_DSN.rsplit("/", 1)[0]
    dsn = f"{base}/{db_name}"
    try:
        yield dsn
    finally:
        # Drop the test DB. Terminate any lingering backends first.
        admin = psycopg.connect(PG_ADMIN_DSN, autocommit=True)
        try:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            admin.close()


# ---------------------------------------------------------------------------
# Temp SQLite source with a known fixture set
# ---------------------------------------------------------------------------
# Two UUID-shaped user ids exercise the TEXT->uuid coercion on users.id and the
# user_id FK columns added by the multi-tenant migration.
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())

NOW_ISO = datetime(2026, 6, 10, 12, 30, 0, tzinfo=timezone.utc).isoformat()

# (product_id, description, brand, category_type, category_override)
PRODUCTS = [
    ("DUMMY-0001", "Organic Whole Milk, 1 gal", "Simple Truth", "routine", 0),
    ("DUMMY-0002", "Extra Virgin Olive Oil, 500ml", "Private Selection", "regular", 1),
    ("DUMMY-0003", "Heirloom Tomatoes, lb", "Fresh", "regular", 0),
]

# Tables we seed and expect at parity in PG (present in BOTH schemas).
SEEDED_TABLES = (
    "users",
    "products",
    "recipes",
    "recipe_ingredients",
    "pantry_items",
    "price_history",
    "safe_products",
)


@pytest.fixture
def sqlite_source() -> Iterator[str]:
    """Build a temp SQLite source DB with the real app schema + fixtures."""
    fd, path = tempfile.mkstemp(prefix="etl_src_", suffix=".db")
    os.close(fd)

    # Point the app's DB_FILE at our temp file, then build the schema with the
    # app's own initializers so we test against the REAL source schema.
    from kroger_mcp.analytics import database as db_mod

    original_db_file = db_mod.DB_FILE
    db_mod.DB_FILE = path
    try:
        # Ensure SQLite backend (no DATABASE_URL) while building the source.
        prior_url = os.environ.pop("DATABASE_URL", None)
        try:
            db_mod.initialize_database()
            db_mod.run_schema_migrations()
            from kroger_mcp.analytics import pg_database as pg_mod

            pg_mod.initialize_sqlite_auth_tables()
        finally:
            if prior_url is not None:
                os.environ["DATABASE_URL"] = prior_url

        _seed_sqlite(path)
        yield path
    finally:
        db_mod.DB_FILE = original_db_file
        if os.path.exists(path):
            os.remove(path)


# Mirrors USER_SCOPED_TABLES in scripts/migrate_to_multi_tenant.py: the real
# cutover adds user_id to these AND backfills every existing row (including the
# bootstrap `default` favorites row and the seeded safety_settings rows) to the
# owner. We reproduce that so no null-user_id rows reach PG's NOT NULL columns.
_USER_SCOPED = (
    "recipes",
    "favorite_lists",
    "safety_settings",
    "pantry_items",
    "safe_products",
    "blocked_products",
    "ingredient_preferences",
)


def _add_user_id(conn: sqlite3.Connection, table: str, owner_id: str) -> None:
    """Add user_id (if missing) and backfill all rows to owner (real migration)."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "user_id" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
    conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (owner_id,))


def _seed_sqlite(path: str) -> None:
    """Insert a known fixture set across an FK chain into the SQLite source."""
    conn = sqlite3.connect(path)
    try:
        # users first (UUID-shaped ids -> PG uuid column) so there is an owner to
        # backfill the user-scoped tables to.
        conn.executemany(
            "INSERT INTO users (id, email, password_hash, display_name, "
            "created_at, last_login_at, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (USER_A, "ada@example.com", "hash_a", "Ada", NOW_ISO, NOW_ISO, 1),
                # is_active 0 exercises the 0 -> False boolean coercion.
                (USER_B, "grace@example.com", "hash_b", "Grace", NOW_ISO, None, 0),
            ],
        )

        # Realistic post-multi-tenant shape: add user_id to every user-scoped
        # table and backfill all existing rows (incl. the auto-seeded `default`
        # favorites row + safety_settings) to USER_A, exactly like the cutover.
        for t in _USER_SCOPED:
            _add_user_id(conn, t, USER_A)

        # products (global; category_override 1/0 -> bool)
        conn.executemany(
            "INSERT INTO products (product_id, description, brand, category_type, "
            "category_override, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (pid, desc, brand, cat, ov, NOW_ISO, NOW_ISO)
                for pid, desc, brand, cat, ov in PRODUCTS
            ],
        )

        # recipes (user-scoped; VARCHAR id)
        conn.execute(
            "INSERT INTO recipes (id, name, description, servings, instructions, "
            "created_at, times_ordered, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "recipe-1",
                "Tomato Salad",
                "Simple summer salad",
                4,
                "Slice; toss.",
                NOW_ISO,
                0,
                USER_A,
            ),
        )

        # recipe_ingredients (FK -> recipes; is_optional bool)
        conn.executemany(
            "INSERT INTO recipe_ingredients (recipe_id, name, quantity, unit, "
            "product_id, is_optional) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("recipe-1", "Heirloom Tomatoes", 2.0, "lb", "DUMMY-0003", 0),
                ("recipe-1", "Olive Oil", 0.25, "cup", "DUMMY-0002", 1),
            ],
        )

        # pantry_items (user-scoped; level_percent int, auto_deplete bool, numeric)
        conn.executemany(
            "INSERT INTO pantry_items (product_id, description, level_percent, "
            "last_updated_at, auto_deplete, daily_depletion_rate, low_threshold, "
            "user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("DUMMY-0001", "Organic Whole Milk, 1 gal", 45, NOW_ISO, 1, 3.5, 20, USER_A),
                ("DUMMY-0003", "Heirloom Tomatoes, lb", 80, NOW_ISO, 0, 0.0, 20, USER_B),
            ],
        )

        # price_history (global; on_sale bool, numeric prices, NOT NULL observed_at)
        conn.executemany(
            "INSERT INTO price_history (product_id, regular_price, sale_price, "
            "on_sale, savings_amount, savings_percent, location_id, observed_at, "
            "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("DUMMY-0001", 4.99, 3.99, 1, 1.0, 20.04, "03400014", NOW_ISO, "scan"),
                ("DUMMY-0002", 12.49, None, 0, 0.0, 0.0, "03400014", NOW_ISO, "scan"),
            ],
        )

        # safe_products (user-scoped)
        conn.execute(
            "INSERT INTO safe_products (product_id, description, brand, added_at, "
            "added_reason, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            ("DUMMY-0001", "Organic Whole Milk", "Simple Truth", NOW_ISO, "clean", USER_A),
        )

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ETL runner helper
# ---------------------------------------------------------------------------
def _load_etl() -> ModuleType:
    """Load the ETL module fresh from its file path (avoids package shadowing)."""
    spec = importlib.util.spec_from_file_location("_etl_under_test", _ETL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass introspection can resolve the module.
    sys.modules["_etl_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _run_etl(source_path: str, dsn: str) -> int:
    """Load the ETL module fresh and run it against the given source/target."""
    os.environ["DATABASE_URL"] = dsn
    etl = _load_etl()
    return int(etl.run_etl(source_path, dsn))


def _count(dsn: str, table: str) -> int:
    """Return the row count of a PG table."""
    conn = psycopg.connect(dsn)
    try:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_etl_parity_and_types(sqlite_source: str, pg_db: str) -> None:
    """ETL reaches parity on every seeded table and preserves types correctly."""
    rc = _run_etl(sqlite_source, pg_db)
    assert rc == 0, "ETL should exit 0 (all migrated tables at parity)"

    # Per-table parity: source count == target count for every seeded table.
    src = sqlite3.connect(sqlite_source)
    try:
        for table in SEEDED_TABLES:
            src_n = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            tgt_n = _count(pg_db, table)
            assert src_n == tgt_n, f"{table}: source {src_n} != target {tgt_n}"
            assert src_n > 0, f"{table}: fixture should have seeded rows"
    finally:
        src.close()

    # Spot-check known rows field-by-field with correct PG types.
    conn = psycopg.connect(pg_db)
    try:
        # users: UUID id survives, is_active 0 -> bool False, 1 -> True.
        row = conn.execute(
            "SELECT id, email, is_active, last_login_at FROM users "
            "WHERE email = 'grace@example.com'"
        ).fetchone()
        assert row is not None
        assert isinstance(row[0], uuid.UUID), "users.id should be a real uuid"
        assert str(row[0]) == USER_B
        assert row[2] is False, "is_active 0 must coerce to bool False"
        assert row[3] is None, "NULL last_login_at must stay NULL"

        row = conn.execute(
            "SELECT is_active FROM users WHERE email = 'ada@example.com'"
        ).fetchone()
        assert row[0] is True, "is_active 1 must coerce to bool True"

        # products: category_override 1 -> True, description preserved as text.
        row = conn.execute(
            "SELECT description, category_override FROM products "
            "WHERE product_id = 'DUMMY-0002'"
        ).fetchone()
        assert row[0] == "Extra Virgin Olive Oil, 500ml"
        assert row[1] is True, "category_override 1 must coerce to bool True"

        # pantry_items: level_percent int + auto_deplete bool + numeric rate.
        row = conn.execute(
            "SELECT level_percent, auto_deplete, daily_depletion_rate, user_id "
            "FROM pantry_items WHERE product_id = 'DUMMY-0001'"
        ).fetchone()
        assert row[0] == 45
        assert row[1] is True
        assert float(row[2]) == 3.5
        assert str(row[3]) == USER_A, "user_id FK must carry the owner uuid"

        # price_history: timestamptz parses to tz-aware datetime; numeric price.
        row = conn.execute(
            "SELECT regular_price, on_sale, observed_at FROM price_history "
            "WHERE product_id = 'DUMMY-0001'"
        ).fetchone()
        assert float(row[0]) == 4.99
        assert row[1] is True
        assert isinstance(row[2], datetime), "observed_at must parse to datetime"
        assert row[2].tzinfo is not None, "timestamptz must be tz-aware"

        # recipe_ingredients: FK chain + numeric quantity + is_optional bool.
        rows = conn.execute(
            "SELECT name, quantity, is_optional FROM recipe_ingredients "
            "WHERE recipe_id = 'recipe-1' ORDER BY name"
        ).fetchall()
        assert len(rows) == 2
        by_name = {r[0]: r for r in rows}
        assert float(by_name["Olive Oil"][1]) == 0.25
        assert by_name["Olive Oil"][2] is True
        assert by_name["Heirloom Tomatoes"][2] is False
    finally:
        conn.close()


def test_etl_idempotent(sqlite_source: str, pg_db: str) -> None:
    """Running the ETL twice leaves target counts unchanged (ON CONFLICT)."""
    rc1 = _run_etl(sqlite_source, pg_db)
    assert rc1 == 0
    first = {t: _count(pg_db, t) for t in SEEDED_TABLES}

    rc2 = _run_etl(sqlite_source, pg_db)
    assert rc2 == 0
    second = {t: _count(pg_db, t) for t in SEEDED_TABLES}

    assert first == second, f"counts changed on re-run: {first} -> {second}"


def test_throwaway_pg_db_is_usable(pg_db: str) -> None:
    """The create/drop lifecycle (mirroring the real cutover) works end to end."""
    conn = psycopg.connect(pg_db)
    try:
        row = conn.execute("SELECT 1").fetchone()
        assert row[0] == 1
    finally:
        conn.close()
