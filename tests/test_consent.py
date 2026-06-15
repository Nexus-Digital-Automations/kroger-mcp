"""
Specs for the data-sharing consent domain (analytics/consent.py) and the
consent-gated shareable-event tracer (analytics/sharing.py).

Consent governs whether any user-derived signal may leave the local database, so
it is a privacy-critical path: these tests pin the opt-in defaults, the gate's
fail-closed behavior, and that withdrawal/deletion actually clear the flags.

Each test runs against an isolated temp SQLite database so nothing touches the
real analytics store.
"""

import sqlite3

import pytest
from _pg_support import skip_on_pg

from kroger_mcp.analytics import consent, database, sharing

# SQLite-specific: monkeypatches database.DB_FILE to an isolated temp SQLite db.
pytestmark = skip_on_pg

USER = "consent-test-user"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point all analytics DB access at a throwaway file with a real schema.

    Re-imports `database` from sys.modules inside the fixture rather than
    relying on the module-level import: another test file
    (test_cart_mark_placed_restock) deletes all kroger_mcp modules from
    sys.modules mid-suite, which would otherwise leave this fixture patching a
    stale module object while `consent` re-imports a fresh one pointed at the
    real DB. Patching the live sys.modules object keeps consent isolated.
    """
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    db_file = tmp_path / "consent_test.db"
    monkeypatch.setattr(db, "DB_FILE", str(db_file))
    db.initialize_database()
    db.run_schema_migrations()  # creates user_settings
    yield str(db_file)


def _enabled_keys(state):
    return {k for k, v in state["categories"].items() if v["enabled"]}


def test_migration_creates_user_settings_table_idempotently(isolated_db):
    database.run_schema_migrations()  # second run must not raise
    with sqlite3.connect(isolated_db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "user_settings" in names


def test_consent_defaults_to_all_off_and_undecided():
    state = consent.get_consent(user_id=USER)
    assert state["decided"] is False
    assert _enabled_keys(state) == set()
    assert set(state["categories"]) == consent._CATEGORY_KEYS


def test_set_consent_enables_only_named_categories_and_marks_decided():
    state = consent.set_consent({"price_observations": True}, user_id=USER)
    assert state["decided"] is True
    assert state["decided_at"]
    assert _enabled_keys(state) == {"price_observations"}


def test_set_consent_rejects_unknown_category():
    with pytest.raises(KeyError):
        consent.set_consent({"not_a_real_category": True}, user_id=USER)


def test_consent_allows_denies_when_category_off():
    assert consent.consent_allows("purchase_patterns", user_id=USER) is False


def test_consent_allows_permits_after_opt_in():
    consent.set_consent({"purchase_patterns": True}, user_id=USER)
    assert consent.consent_allows("purchase_patterns", user_id=USER) is True


def test_consent_allows_unknown_category_fails_closed():
    assert consent.consent_allows("ghost_category", user_id=USER) is False


def test_withdraw_consent_disables_all_but_keeps_decided():
    consent.set_consent({"purchase_patterns": True, "recipe_trends": True}, user_id=USER)
    state = consent.withdraw_consent(user_id=USER)
    assert state["decided"] is True
    assert _enabled_keys(state) == set()


def test_delete_shared_data_withdraws_and_reports_zero_purged():
    consent.set_consent({"consumption": True}, user_id=USER)
    result = consent.delete_shared_data(user_id=USER)
    assert result["deleted"] is True
    assert result["purged_rows"] == 0
    assert _enabled_keys(result["consent"]) == set()


def test_record_shareable_event_noops_when_consent_denied():
    assert sharing.record_shareable_event("recipe_trends", {"x": 1}, user_id=USER) is False


def test_record_shareable_event_accepts_when_consent_granted():
    consent.set_consent({"recipe_trends": True}, user_id=USER)
    assert sharing.record_shareable_event("recipe_trends", {"x": 1}, user_id=USER) is True


def test_set_consent_does_not_disturb_unrelated_preferences():
    from kroger_mcp.tools import shared

    shared._save_preference("default_servings_per_meal", 6, user_id=USER)
    consent.set_consent({"price_observations": True}, user_id=USER)
    assert shared._load_preferences(user_id=USER)["default_servings_per_meal"] == 6
