"""Regression coverage for the SQLite->Postgres boolean-literal rewrite.

_normalize_bool_literals (database.py) rewrites `bool_col = 0|1` to
`bool_col = FALSE|TRUE` for the PG path, driven by an explicit allowlist
(_PG_BOOL_COLS) that must stay in sync with pg_database.SCHEMA_SQL's BOOLEAN
columns. `cook_skipped` was added to meal_entries as BOOLEAN in Postgres but
never added to that allowlist, so list_pending_meals' `me.cook_skipped = 0`
raised `psycopg.errors.UndefinedFunction: operator does not exist: boolean =
integer` on every call in production — silently, since test_meal_reconcile.py
(which exercises list_pending_meals) is skip_on_pg and this pure function had
zero direct test coverage of its own. This is a no-DB pure-function test, so
it runs everywhere (no local Postgres required) and catches the next such gap.
"""

import re

from kroger_mcp.analytics.database import _PG_BOOL_COLS, _normalize_bool_literals
from kroger_mcp.analytics.pg_database import _PG_COLUMN_MIGRATIONS, SCHEMA_SQL


def test_cook_skipped_is_normalized():
    """The exact query shape that broke list_pending_meals in production."""
    sql = (
        "SELECT me.plan_id FROM meal_entries me "
        "WHERE me.user_id = ? AND me.pantry_deducted = 0 AND me.cook_skipped = 0 "
        "AND me.cooked_at IS NULL AND me.meal_date < ?"
    )
    out = _normalize_bool_literals(sql)
    assert "cook_skipped = FALSE" in out
    assert "pantry_deducted = FALSE" in out
    assert "= 0" not in out


def test_every_pg_bool_col_rewrites_both_literals():
    for col in _PG_BOOL_COLS:
        assert _normalize_bool_literals(f"x.{col} = 1") == f"x.{col} = TRUE"
        assert _normalize_bool_literals(f"x.{col} = 0") == f"x.{col} = FALSE"


def test_parameterized_values_are_untouched():
    """Only literal 0/1 are rewritten — bound params (`= %s`/`= ?`) must not be."""
    sql = "UPDATE meal_entries SET cook_skipped = ? WHERE id = ?"
    assert _normalize_bool_literals(sql) == sql


def test_pg_bool_cols_allowlist_covers_every_boolean_column_in_pg_schema():
    """Every BOOLEAN column declared in pg_database.py must be in _PG_BOOL_COLS,
    or a future `col = 0|1` query against it will silently 500 on Postgres —
    exactly the bug this module regression-tests for cook_skipped.

    Columns intentionally kept INTEGER (not BOOLEAN) in PG are out of scope by
    construction (they're never declared BOOLEAN, so they never appear here).
    """
    # Strip `-- ...` comments first (e.g. "kept INTEGER (not BOOLEAN)" reads as
    # a bare word before BOOLEAN otherwise, and isn't a column declaration).
    lines = (SCHEMA_SQL + "\n" + "\n".join(_PG_COLUMN_MIGRATIONS)).splitlines()
    combined = "\n".join(line.split("--", 1)[0] for line in lines)
    declared_boolean_cols = set(re.findall(r"(\w+)\s+BOOLEAN\b", combined))

    missing = declared_boolean_cols - set(_PG_BOOL_COLS)
    assert not missing, (
        f"BOOLEAN column(s) declared in pg_database.py but missing from "
        f"database._PG_BOOL_COLS: {sorted(missing)}"
    )
