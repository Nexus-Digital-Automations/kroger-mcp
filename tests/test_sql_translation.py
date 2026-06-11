"""Boolean-literal normalization in the SQLite->Postgres SQL adapter.

The codebase was written for SQLite and compares BOOLEAN columns with the integer
idiom (`col = 1`, `SET col = 0`). Postgres rejects `boolean = integer`, which broke
auth (`users.is_active = 1`) and several features after the prod cutover. The
adapter now rewrites those literals to TRUE/FALSE for the PG path only.
"""

from __future__ import annotations

from kroger_mcp.analytics.database import _translate_sql


def test_bool_eq_one_becomes_true():
    assert "is_active = TRUE" in _translate_sql(
        "SELECT 1 FROM users WHERE is_active = 1"
    )


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
