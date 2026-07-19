"""Tests for per-account ingredient link memory and smart suggestions.

Owns: the data-integrity guarantees of analytics.ingredient_links —
mechanical name grouping, upsert counting, account isolation, learned
canonical-name standardization, and suggestion ranking. These back the recipe
linking popover's "your usuals" / auto-link and must not leak across accounts.

@stable
"""

from __future__ import annotations

import uuid

import pytest
from _pg_support import RUNNING_ON_PG

from kroger_mcp.analytics import ingredient_links as il
from kroger_mcp.analytics.database import ensure_initialized, get_db_connection


@pytest.fixture
def two_users(tmp_path, monkeypatch):
    """Two throwaway account ids; teardown purges their rows.

    Random UUIDs keep these isolated from real data, but on Postgres
    ingredient_links.user_id has an FK to users(id), so an isolated SQLite
    DB is still needed to avoid touching the real schema. Was previously
    unisolated — see test_pantry_expiration.py's clean_db.
    """
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    if not RUNNING_ON_PG:
        monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "ingredient_links_test.db"))
    ensure_initialized()
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    if RUNNING_ON_PG:
        conn = get_db_connection()
        try:
            for uid in (a_id, b_id):
                conn.execute(
                    "INSERT INTO users (id, email, password_hash, display_name) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
                    (uid, f"{uid}@tests.local", "x", "Test User"),
                )
            conn.commit()
        finally:
            conn.close()
    try:
        yield a_id, b_id
    finally:
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM ingredient_links WHERE user_id IN (?, ?)", (a_id, b_id)
        )
        if RUNNING_ON_PG:
            # FK is ON DELETE CASCADE, so this also clears any leftover links.
            conn.execute("DELETE FROM users WHERE id IN (?, ?)", (a_id, b_id))
        conn.commit()
        conn.close()


class TestNormalize:
    """normalize_ingredient_name is mechanical-only — no curated dictionary."""

    def test_case_and_whitespace_collapse(self):
        assert il.normalize_ingredient_name("  Yellow   Onion ") == "yellow onion"

    def test_trailing_plural_is_singularized(self):
        assert il.normalize_ingredient_name("yellow onions") == "yellow onion"
        assert il.normalize_ingredient_name("tomatoes") == "tomato"

    def test_punctuation_stripped(self):
        assert il.normalize_ingredient_name("parsley, fresh!") == "parsley fresh"

    def test_blank_is_empty(self):
        assert il.normalize_ingredient_name("") == ""
        assert il.normalize_ingredient_name(None) == ""  # type: ignore[arg-type]

    def test_adjectives_are_preserved(self):
        # "fresh parsley" must NOT collapse to "parsley" mechanically — the
        # distinction is reconciled later via the shared product, not a list.
        assert il.normalize_ingredient_name("fresh parsley") == "fresh parsley"


class TestRecordLink:
    """record_link upserts on (user_id, norm_name, product_id)."""

    def test_first_link_inserts_then_repeat_increments(self, two_users):
        a_id, _ = two_users
        il.record_link(a_id, "Yellow Onion", "PROD_ONION", "Yellow Onion 3lb")
        il.record_link(a_id, "yellow onions", "PROD_ONION")  # de-plural -> same row

        sugg = il.suggest_products_for_ingredient(a_id, "yellow onion")
        onion = next(s for s in sugg if s["product_id"] == "PROD_ONION")
        assert onion["times_linked"] == 2

    def test_blank_inputs_are_ignored(self, two_users):
        a_id, _ = two_users
        il.record_link(a_id, "", "PROD_X")
        il.record_link(a_id, "salt", "")
        assert il.suggest_products_for_ingredient(a_id, "salt") == []


class TestSuggestionsAndIsolation:
    def test_prior_link_is_suggested_with_best_guess(self, two_users):
        a_id, _ = two_users
        il.record_link(a_id, "yellow onion", "PROD_ONION", "Simple Truth Yellow Onion")
        il.record_link(a_id, "yellow onion", "PROD_ONION")

        sugg = il.suggest_products_for_ingredient(a_id, "yellow onion")
        assert sugg and sugg[0]["product_id"] == "PROD_ONION"
        guess = il.best_guess(sugg)
        assert guess is not None and guess["product_id"] == "PROD_ONION"

    def test_account_isolation(self, two_users):
        a_id, b_id = two_users
        il.record_link(a_id, "yellow onion", "PROD_A")
        il.record_link(b_id, "yellow onion", "PROD_B")

        a_ids = {s["product_id"] for s in il.suggest_products_for_ingredient(a_id, "yellow onion")}
        b_ids = {s["product_id"] for s in il.suggest_products_for_ingredient(b_id, "yellow onion")}
        assert a_ids == {"PROD_A"}
        assert b_ids == {"PROD_B"}

    def test_cold_start_returns_empty(self, two_users):
        a_id, _ = two_users
        assert il.suggest_products_for_ingredient(a_id, "rutabaga") == []
        assert il.best_guess([]) is None


class TestCanonicalName:
    """Standardization is learned from history via the shared product."""

    def test_variant_standardizes_to_most_used_form(self, two_users):
        a_id, _ = two_users
        # Both forms link to the same product; "parsley" is written more often.
        il.record_link(a_id, "fresh parsley", "PROD_PARS", "Organic Parsley")
        il.record_link(a_id, "parsley", "PROD_PARS")
        il.record_link(a_id, "parsley", "PROD_PARS")

        canon = il.get_canonical_name(a_id, "fresh parsley")
        assert canon is not None
        assert canon["canonical_name"] == "parsley"
        assert 0 < canon["confidence"] <= 1

    def test_already_standard_returns_none(self, two_users):
        a_id, _ = two_users
        il.record_link(a_id, "parsley", "PROD_PARS")
        il.record_link(a_id, "parsley", "PROD_PARS")
        assert il.get_canonical_name(a_id, "parsley") is None

    def test_thin_history_returns_none(self, two_users):
        a_id, _ = two_users
        il.record_link(a_id, "fresh parsley", "PROD_PARS")  # single link, no support
        assert il.get_canonical_name(a_id, "fresh parsley") is None
