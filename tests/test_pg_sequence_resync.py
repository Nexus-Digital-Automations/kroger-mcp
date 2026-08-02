"""Tests for scripts/pg_sequence_resync.py (requires a local Postgres).

Guards the data-integrity bug this module exists to prevent: rows migrated with
explicit ``id`` values never advance the owning sequence, so the next
``nextval()``-based insert collides and raises ``UniqueViolation``. A regression
here silently breaks every insert into a migrated table, which is exactly how it
went unnoticed in production for weeks.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "pg_sequence_resync.py"
_spec = importlib.util.spec_from_file_location("pg_sequence_resync", _MODULE_PATH)
assert _spec and _spec.loader
seqmod = importlib.util.module_from_spec(_spec)
sys.modules["pg_sequence_resync"] = seqmod
_spec.loader.exec_module(seqmod)

psycopg = pytest.importorskip("psycopg")

PG_ADMIN_DSN = os.environ.get("ETL_TEST_PG_ADMIN", "postgresql://localhost:5432/postgres")


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
def pg_conn() -> Iterator[psycopg.Connection]:
    """Create a throwaway PG database, yield a connection, drop it in teardown."""
    db_name = f"seqtest_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(PG_ADMIN_DSN, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        admin.close()

    dsn = f"{PG_ADMIN_DSN.rsplit('/', 1)[0]}/{db_name}"
    conn = psycopg.connect(dsn)
    try:
        yield conn
    finally:
        conn.close()
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


def _seed_desynced(conn: psycopg.Connection, max_id: int = 500) -> None:
    """Create a SERIAL table whose rows were inserted with explicit ids.

    Reproduces exactly what the ETL does: the sequence is left at its start
    value while the table's max id is far ahead.
    """
    conn.execute("CREATE TABLE widgets (id SERIAL PRIMARY KEY, label TEXT)")
    conn.execute(
        "INSERT INTO widgets (id, label) SELECT g, 'w' || g FROM generate_series(1, %s) g",
        (max_id,),
    )
    conn.commit()


def test_desynced_sequence_breaks_inserts_then_resync_repairs_it(
    pg_conn: psycopg.Connection,
) -> None:
    """The bug reproduces, and resync_sequences fixes it."""
    _seed_desynced(pg_conn)

    # Precondition: the untouched sequence collides with a migrated row.
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_conn.execute("INSERT INTO widgets (label) VALUES ('boom')")
    pg_conn.rollback()

    assert seqmod.resync_sequences(pg_conn) == 1

    # The same insert now succeeds and lands immediately after the real max.
    row = pg_conn.execute("INSERT INTO widgets (label) VALUES ('ok') RETURNING id").fetchone()
    assert row is not None
    assert row[0] == 501


def test_resync_never_moves_a_sequence_backward(pg_conn: psycopg.Connection) -> None:
    """A sequence ahead of max(id) is left alone — lowering it could collide."""
    _seed_desynced(pg_conn, max_id=10)
    pg_conn.execute("SELECT setval('widgets_id_seq', 9999, true)")
    pg_conn.commit()

    assert seqmod.resync_sequences(pg_conn) == 0

    row = pg_conn.execute("SELECT last_value FROM widgets_id_seq").fetchone()
    assert row is not None
    assert row[0] == 9999


def test_resync_is_idempotent(pg_conn: psycopg.Connection) -> None:
    """A second run advances nothing — the first already reached the max."""
    _seed_desynced(pg_conn)

    assert seqmod.resync_sequences(pg_conn) == 1
    assert seqmod.resync_sequences(pg_conn) == 0


def test_resync_ignores_tables_without_owned_sequences(
    pg_conn: psycopg.Connection,
) -> None:
    """A table with no SERIAL/IDENTITY column contributes no sequence to fix."""
    pg_conn.execute("CREATE TABLE plain (sku TEXT PRIMARY KEY, label TEXT)")
    pg_conn.execute("INSERT INTO plain VALUES ('A-1', 'first')")
    pg_conn.commit()

    assert seqmod.resync_sequences(pg_conn) == 0


def test_resync_handles_an_empty_migrated_table(pg_conn: psycopg.Connection) -> None:
    """An empty table's sequence is already correct and must not be touched."""
    pg_conn.execute("CREATE TABLE empties (id SERIAL PRIMARY KEY, label TEXT)")
    pg_conn.commit()

    assert seqmod.resync_sequences(pg_conn) == 0

    # The first real insert still starts at 1.
    row = pg_conn.execute("INSERT INTO empties (label) VALUES ('x') RETURNING id").fetchone()
    assert row is not None
    assert row[0] == 1
