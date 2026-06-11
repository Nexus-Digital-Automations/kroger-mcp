"""PG ⇄ SQLite schema parity for the six previously-missing user-data tables.

The PostgreSQL ``SCHEMA_SQL`` in ``kroger_mcp.analytics.pg_database`` was missing
six tables that exist in the SQLite schema:

    user_settings, ingredient_links, custom_ingredients,
    ingredient_overrides, pending_gaps, cook_deductions

A fresh production PG built from ``initialize_pg_database()`` therefore lacked
them entirely, and the SQLite→PG ETL silently dropped that data (consent/
preferences, custom ingredients, ingredient overrides, ingredient↔product link
memory, recipe-gap reconciliation, cook deductions).

This module proves, against a throwaway *local* Postgres database:
  * every previously-missing table now exists after initialize_pg_database()
  * the user_settings composite PK (user_id, setting_key) works as the exact
    ON CONFLICT upsert target the consent layer's _save_preference relies on —
    inserted directly through the real PG cursor (the path the web/auth/ETL
    layers use), proving the consent storage shape works on PG
  * a custom_ingredients write succeeds against the new PG table

If a local Postgres is not reachable the whole module is skipped (not failed).
The test PG database is dropped in teardown even on failure.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

# psycopg is a hard project dependency (the PG backend uses it).
psycopg = pytest.importorskip("psycopg")

PG_ADMIN_DSN = os.environ.get("ETL_TEST_PG_ADMIN", "postgresql://localhost:5432/postgres")

# The six tables that were absent from PG SCHEMA_SQL — the bug under test.
PREVIOUSLY_MISSING_TABLES = (
    "user_settings",
    "ingredient_links",
    "custom_ingredients",
    "ingredient_overrides",
    "pending_gaps",
    "cook_deductions",
)


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
def pg_dsn() -> Iterator[str]:
    """Create a throwaway PG database; yield its DSN; drop it in teardown."""
    db_name = f"pg_parity_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(PG_ADMIN_DSN, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        admin.close()

    base = PG_ADMIN_DSN.rsplit("/", 1)[0]
    dsn = f"{base}/{db_name}"
    try:
        yield dsn
    finally:
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


@pytest.fixture
def initialized_pg(pg_dsn: str) -> Iterator[str]:
    """Point the PG backend at the throwaway DB and run initialize_pg_database().

    Resets the lazy connection-pool singleton and DATABASE_URL around the test so
    the rest of the suite is unaffected.
    """
    from kroger_mcp.analytics import pg_database

    prev_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_dsn
    # Force a fresh pool bound to the throwaway DB.
    pg_database.close_pool()
    try:
        pg_database.initialize_pg_database()
        yield pg_dsn
    finally:
        pg_database.close_pool()
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url


def _table_exists(dsn: str, table: str) -> bool:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        ).fetchone()
    return row is not None


def _seed_user(dsn: str) -> str:
    """Insert a minimal valid users row; return its uuid as a string."""
    user_id = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, f"{user_id}@example.test", "x", "Parity Tester"),
        )
    return user_id


# ---------------------------------------------------------------------------
# Schema existence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", PREVIOUSLY_MISSING_TABLES)
def test_previously_missing_table_now_exists_in_pg(initialized_pg: str, table: str):
    """Each of the six tables is created by initialize_pg_database()."""
    assert _table_exists(initialized_pg, table), (
        f"{table} missing from PG schema after initialize_pg_database()"
    )


def test_user_settings_pk_is_user_id_setting_key(initialized_pg: str):
    """The composite PK must be exactly (user_id, setting_key).

    This is the conflict target _save_preference relies on:
    ON CONFLICT(user_id, setting_key). A wrong/missing constraint breaks PG
    upserts at runtime.
    """
    with psycopg.connect(initialized_pg) as conn:
        rows = conn.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = 'public'
              AND tc.table_name = 'user_settings'
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
            """
        ).fetchall()
    assert [r[0] for r in rows] == ["user_id", "setting_key"]


# ---------------------------------------------------------------------------
# Functional proof on the PG backend — the real bug
# ---------------------------------------------------------------------------
def test_user_settings_upsert_works_on_pg(initialized_pg: str):
    """Prove the consent storage shape (user_settings ON CONFLICT upsert) works.

    This exercises the exact INSERT ... ON CONFLICT(user_id, setting_key) the
    consent layer (_save_preference) writes — directly via the real PG cursor,
    which is the connection path the web/auth/ETL layers use against PG. Before
    this fix the table did not exist, so this would raise UndefinedTable.
    """
    from kroger_mcp.analytics import pg_database

    user_id = _seed_user(initialized_pg)

    upsert = """
        INSERT INTO user_settings (user_id, setting_key, setting_value, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id, setting_key) DO UPDATE SET
            setting_value = EXCLUDED.setting_value,
            updated_at = EXCLUDED.updated_at
    """

    # First write decides + enables one category, mirroring set_consent.
    with pg_database.get_pg_cursor() as cur:
        cur.execute(upsert, (user_id, "consent_decided", "True"))
        cur.execute(upsert, (user_id, "consent_price_observations", "True"))

    # Second write flips the flag via the same conflict target (proves upsert).
    with pg_database.get_pg_cursor() as cur:
        cur.execute(upsert, (user_id, "consent_price_observations", "False"))

    with psycopg.connect(initialized_pg) as conn:
        rows = dict(
            conn.execute(
                "SELECT setting_key, setting_value FROM user_settings WHERE user_id = %s",
                (user_id,),
            ).fetchall()
        )

    assert rows["consent_decided"] == "True"
    # The upsert updated the existing row rather than inserting a duplicate.
    assert rows["consent_price_observations"] == "False"


def test_consent_storage_shape_round_trips_on_pg(initialized_pg: str):
    """End-to-end consent semantics over the PG user_settings table.

    Reads the per-user settings back exactly as _load_preferences would and
    reconstructs the consent verdict: decided=True with the named category on.
    """
    from kroger_mcp.analytics import pg_database

    user_id = _seed_user(initialized_pg)
    upsert = """
        INSERT INTO user_settings (user_id, setting_key, setting_value, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id, setting_key) DO UPDATE SET
            setting_value = EXCLUDED.setting_value
    """
    with pg_database.get_pg_cursor() as cur:
        cur.execute(upsert, (user_id, "consent_decided", "True"))
        cur.execute(upsert, (user_id, "consent_price_observations", "True"))

    with psycopg.connect(initialized_pg) as conn:
        prefs = dict(
            conn.execute(
                "SELECT setting_key, setting_value FROM user_settings WHERE user_id = %s",
                (user_id,),
            ).fetchall()
        )

    decided = prefs.get("consent_decided") == "True"
    price_enabled = prefs.get("consent_price_observations") == "True"
    assert decided is True
    assert price_enabled is True


def test_custom_ingredient_write_on_pg(initialized_pg: str):
    """A user-scoped custom_ingredients insert succeeds and round-trips.

    Also proves the UNIQUE(user_id, ingredient_name) constraint allows the same
    name under a different user but rejects a duplicate for the same user.
    """
    from kroger_mcp.analytics import pg_database

    user_a = _seed_user(initialized_pg)
    user_b = _seed_user(initialized_pg)

    insert = """
        INSERT INTO custom_ingredients (user_id, ingredient_name, severity, reason)
        VALUES (%s, %s, %s, %s)
    """
    with pg_database.get_pg_cursor() as cur:
        cur.execute(insert, (user_a, "carrageenan", "warning", "gut-barrier disruption"))
        # Same name, different user — allowed by the user-scoped unique key.
        cur.execute(insert, (user_b, "carrageenan", "warning", "gut-barrier disruption"))

    with psycopg.connect(initialized_pg) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM custom_ingredients WHERE ingredient_name = %s",
            ("carrageenan",),
        ).fetchone()
        assert count is not None
        assert count[0] == 2
        # is_active defaulted to BOOLEAN TRUE (INTEGER 1 in SQLite).
        active = conn.execute(
            "SELECT is_active FROM custom_ingredients WHERE user_id = %s",
            (user_a,),
        ).fetchone()
        assert active is not None
        assert active[0] is True

    # A duplicate for the SAME user must violate the unique constraint.
    with pytest.raises(psycopg.errors.UniqueViolation):
        with pg_database.get_pg_cursor() as cur:
            cur.execute(insert, (user_a, "carrageenan", "warning", "dup"))
