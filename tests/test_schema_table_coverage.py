"""Schema parity guard: every table the live SQLite runtime creates must also
exist in the Postgres ``SCHEMA_SQL``.

The SQLite→Postgres ETL (``scripts/etl_sqlite_to_pg.py``) migrates only tables
present in BOTH backends. A table that exists in the running SQLite app but is
missing from ``pg_database.SCHEMA_SQL`` would therefore be (a) silently dropped
on migration and (b) crash the app on its first write under Postgres. This test
builds the real SQLite schema and fails loudly on any such drift.

No Postgres is required — it parses ``SCHEMA_SQL`` as text — so it always runs.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from _pg_support import skip_on_pg

# The six tables reconciled into the PG schema during the Phase 3 migration prep
# — kept as an explicit sentinel so an accidental revert is caught immediately.
# whole_foods_catalog + deal_scan_results are still created by the current SQLite
# code; the other four are PROD-LEGACY (created by an older app version, present
# on the production DB but no longer in the current schema/writers). All six must
# exist in PG SCHEMA_SQL so the production migration doesn't silently drop their
# rows (user_notion_sync holds real data on prod).
_RECONCILED = {
    "whole_foods_catalog",
    "deal_scan_results",
    "meal_log",
    "meal_log_items",
    "pantry_consumption_log",
    "user_notion_sync",
}


def _sqlite_runtime_tables(tmp_path: Path) -> set[str]:
    """Build the full SQLite schema at a throwaway path and return its tables."""
    import kroger_mcp.analytics.database as db_mod
    from kroger_mcp.analytics.pg_database import initialize_sqlite_auth_tables

    path = str(tmp_path / "parity.db")
    original = db_mod.DB_FILE
    db_mod.DB_FILE = path
    try:
        db_mod.initialize_database()  # base analytics schema
        db_mod.run_schema_migrations()  # ALTERs + user_id rebuilds
        initialize_sqlite_auth_tables()  # users / user_sessions / kroger_tokens
        with sqlite3.connect(path) as raw:
            rows = raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return {r[0] for r in rows}
    finally:
        db_mod.DB_FILE = original


def _pg_schema_tables() -> set[str]:
    from kroger_mcp.analytics.pg_database import SCHEMA_SQL

    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_SQL))


# SQLite-specific: builds the live schema via SQLite DDL (AUTOINCREMENT) at runtime.
@skip_on_pg
def test_pg_schema_covers_every_sqlite_table(tmp_path):
    sqlite_tables = _sqlite_runtime_tables(tmp_path)
    pg_tables = _pg_schema_tables()
    missing = sqlite_tables - pg_tables
    assert not missing, (
        f"pg_database.SCHEMA_SQL is missing {len(missing)} table(s) that the "
        f"SQLite runtime creates: {sorted(missing)}. Add them to SCHEMA_SQL or "
        f"the SQLite->Postgres ETL will silently drop their rows."
    )


def test_reconciled_tables_present_in_pg_schema():
    """All six reconciled tables must be in PG so prod's rows migrate intact.

    Four are prod-legacy (not created by the current SQLite code), so this guard
    is PG-side only — it asserts the migration target can receive every table the
    production database actually has.
    """
    pg_tables = _pg_schema_tables()
    assert _RECONCILED <= pg_tables, (
        f"PG SCHEMA_SQL is missing reconciled table(s): "
        f"{sorted(_RECONCILED - pg_tables)}"
    )
