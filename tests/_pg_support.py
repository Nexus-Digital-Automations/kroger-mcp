"""Shared Postgres test-mode helpers.

``RUNNING_ON_PG`` is true when the whole suite is launched against Postgres
(``DATABASE_URL`` set at process start, e.g. via ``scripts/test_on_pg.sh``).
``skip_on_pg`` marks tests that exercise SQLite-specific behaviour — SQLite DDL
(``AUTOINCREMENT``), ``sqlite_master``/``PRAGMA`` introspection, ``DB_FILE``
monkeypatching, or the SQLite migration path. Those are tests OF the SQLite
backend and are not meaningful on Postgres, so they are skipped there rather
than rewritten.
"""

import os

import pytest

RUNNING_ON_PG = bool(os.environ.get("DATABASE_URL"))

skip_on_pg = pytest.mark.skipif(
    RUNNING_ON_PG,
    reason="SQLite-specific (DDL/migration/DB_FILE) — not applicable on Postgres",
)
