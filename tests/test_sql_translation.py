"""Boolean-literal normalization in the SQLite->Postgres SQL adapter.

The codebase was written for SQLite and compares BOOLEAN columns with the integer
idiom (`col = 1`, `SET col = 0`). Postgres rejects `boolean = integer`, which broke
auth (`users.is_active = 1`) and several features after the prod cutover. The
adapter now rewrites those literals to TRUE/FALSE for the PG path only.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from uuid import UUID

from kroger_mcp.analytics.database import _coerce_pg_value, _translate_sql


def test_bool_eq_one_becomes_true():
    assert "is_active = TRUE" in _translate_sql(
        "SELECT 1 FROM users WHERE is_active = 1"
    )


# --- PG-native value coercion (parity with SQLite's TEXT/REAL representation) ---
# psycopg returns datetime/Decimal/UUID objects where SQLite returns str/float.
# Returning those raw 500s any JSON endpoint (e.g. added_at in get_safe_products
# after the prod cutover). _coerce_pg_value normalises them; these pin that.


def test_coerce_datetime_to_string():
    out = _coerce_pg_value(dt.datetime(2026, 6, 16, 3, 4, 5))
    assert out == "2026-06-16 03:04:05" and isinstance(out, str)


def test_coerce_date_and_time_to_string():
    assert _coerce_pg_value(dt.date(2026, 6, 16)) == "2026-06-16"
    assert _coerce_pg_value(dt.time(3, 4, 5)) == "03:04:05"


def test_coerce_decimal_to_float():
    out = _coerce_pg_value(Decimal("4.99"))
    assert out == 4.99 and isinstance(out, float)


def test_coerce_uuid_to_string():
    u = UUID("12345678-1234-5678-1234-567812345678")
    assert _coerce_pg_value(u) == str(u)


def test_coerce_leaves_bool_and_primitives():
    assert _coerce_pg_value(True) is True
    assert _coerce_pg_value(1) == 1
    assert _coerce_pg_value("x") == "x"
    assert _coerce_pg_value(None) is None


def test_coerced_row_is_json_serializable():
    # The exact failure mode the cutover hit: a row with a timestamp/decimal/uuid.
    row = {
        "product_id": "X",
        "added_at": _coerce_pg_value(dt.datetime(2026, 6, 16, 3, 4, 5)),
        "price": _coerce_pg_value(Decimal("4.99")),
        "user_id": _coerce_pg_value(UUID("12345678-1234-5678-1234-567812345678")),
    }
    json.dumps(row)  # must not raise


def test_bool_eq_zero_becomes_false():
    assert "is_template = FALSE" in _translate_sql(
        "SELECT * FROM meal_plans WHERE is_template = 0"
    )


def test_set_bool_update_becomes_true():
    assert "pantry_deducted = TRUE" in _translate_sql(
        "UPDATE meal_entries SET pantry_deducted = 1 WHERE id = ?"
    )


def test_set_bool_update_becomes_false():
    out = _translate_sql(
        "UPDATE meal_entries SET cooked_at = NULL, pantry_deducted = 0 WHERE id = ?"
    )
    assert "pantry_deducted = FALSE" in out


def test_category_override_or_null_preserved():
    out = _translate_sql(
        "WHERE p.category_override = 0 OR p.category_override IS NULL"
    )
    assert "category_override = FALSE OR" in out
    assert "category_override IS NULL" in out


def test_integer_columns_not_rewritten():
    # is_currently_available + viewed are INTEGER in PG — keep `= 1`.
    out = _translate_sql("SELECT * FROM whole_foods_catalog WHERE is_currently_available = 1")
    assert "is_currently_available = 1" in out
    assert "TRUE" not in out


def test_non_boolean_equality_untouched():
    out = _translate_sql("SELECT * FROM x WHERE id = 1 AND quantity = 0")
    assert "id = 1" in out and "quantity = 0" in out


def test_parameterized_value_not_rewritten():
    # `= ?` -> `= %s`; a parameter is not a literal 0/1, so no TRUE/FALSE.
    out = _translate_sql("UPDATE users SET is_active = ? WHERE id = ?")
    assert "is_active = %s" in out
    assert "TRUE" not in out and "FALSE" not in out


def test_auth_session_query_normalized():
    out = _translate_sql(
        "SELECT u.id FROM user_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND u.is_active = 1"
    )
    assert "u.is_active = TRUE" in out
    assert "s.token_hash = %s" in out
