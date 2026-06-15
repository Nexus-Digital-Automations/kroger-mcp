"""
User-isolation tests for the multi-tenant migration.

Owns: enforcement that user A's data is unreachable from user B's session,
across the analytics layer (direct function calls with user_id arg) and the
HTTP layer (FastAPI TestClient with session cookies).

@stable
"""

from __future__ import annotations

import uuid

import pytest

from kroger_mcp.analytics import favorites
from kroger_mcp.analytics.database import get_db_connection
from kroger_mcp.auth.passwords import hash_password


@pytest.fixture
def two_users():
    """Create two throwaway users in the live analytics DB and yield their ids.

    Teardown deletes any rows owned by either user across all user-scoped
    tables so tests cannot pollute each other or jeremyparker's data.
    """
    conn = get_db_connection()
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
            (a_id, f"a-{a_id}@test", hash_password("pw"), "userA"),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
            (b_id, f"b-{b_id}@test", hash_password("pw"), "userB"),
        )
        conn.commit()
        yield a_id, b_id
    finally:
        # favorite_list_items is NOT user-keyed (it has no usable user_id column —
        # it is scoped via favorite_lists.id with ON DELETE CASCADE), so deleting
        # favorite_lists below removes its items. Listing it here errored on
        # Postgres (no such column) and was a no-op on SQLite.
        for table in (
            "favorite_lists",
            "pantry_items",
            "pantry_consumption_log",
            "meal_entries",
            "meal_plans",
            "safe_products",
            "blocked_products",
            "custom_ingredients",
            "ingredient_overrides",
            "ingredient_preferences",
            "safety_settings",
            "deal_watchlist",
            "user_carts",
            "user_shopping_lists",
            "user_settings",
        ):
            conn.execute(f"DELETE FROM {table} WHERE user_id IN (?, ?)", (a_id, b_id))
        conn.execute("DELETE FROM users WHERE id IN (?, ?)", (a_id, b_id))
        conn.commit()
        conn.close()


class TestFavoritesScoping:
    """Favorite-list operations must enforce user_id boundaries."""

    def test_get_lists_does_not_leak_across_users(self, two_users):
        """Spec: user A's favorites lists are invisible from user B's get_lists()."""
        a_id, b_id = two_users
        favorites.create_list(name="A-list", user_id=a_id)

        b_lists = favorites.get_lists(user_id=b_id)
        b_names = [lst["name"] for lst in b_lists]
        assert "A-list" not in b_names

    def test_get_list_by_id_returns_none_for_other_user(self, two_users):
        """Spec: requesting another user's list by id returns None (not 403, not 404)."""
        a_id, b_id = two_users
        created = favorites.create_list(name="A-private", user_id=a_id)
        list_id = created["list_id"]

        assert favorites.get_list(list_id, user_id=a_id) is not None
        assert favorites.get_list(list_id, user_id=b_id) is None

    def test_delete_list_refuses_other_users_list(self, two_users):
        """Spec: deleting another user's list returns success=False."""
        a_id, b_id = two_users
        created = favorites.create_list(name="A-shielded", user_id=a_id)
        list_id = created["list_id"]

        b_attempt = favorites.delete_list(list_id, user_id=b_id)
        assert b_attempt["success"] is False

        a_attempt = favorites.delete_list(list_id, user_id=a_id)
        assert a_attempt["success"] is True

    def test_each_user_gets_their_own_default_list(self, two_users):
        """Spec: get_lists auto-creates 'My Favorites' for a brand-new user."""
        a_id, b_id = two_users
        a_lists = favorites.get_lists(user_id=a_id)
        b_lists = favorites.get_lists(user_id=b_id)

        a_default_ids = {lst["id"] for lst in a_lists if lst.get("is_default")}
        b_default_ids = {lst["id"] for lst in b_lists if lst.get("is_default")}
        assert a_default_ids and b_default_ids
        assert a_default_ids.isdisjoint(b_default_ids)


def test_default_user_id_requires_env(monkeypatch):
    """Spec: default_user_id raises if KROGER_MCP_DEFAULT_USER_ID is unset."""
    from kroger_mcp.auth.dependencies import default_user_id

    monkeypatch.delenv("KROGER_MCP_DEFAULT_USER_ID", raising=False)
    with pytest.raises(RuntimeError, match="KROGER_MCP_DEFAULT_USER_ID"):
        default_user_id()


def test_default_user_id_returns_env_value(monkeypatch):
    """Spec: default_user_id returns the migration-installed owner id."""
    from kroger_mcp.auth.dependencies import default_user_id

    monkeypatch.setenv("KROGER_MCP_DEFAULT_USER_ID", "test-owner-uuid")
    assert default_user_id() == "test-owner-uuid"


def test_resolve_user_id_falls_back_to_default(monkeypatch):
    """Spec: when None is passed to analytics, default_user_id() supplies the owner."""
    monkeypatch.setenv("KROGER_MCP_DEFAULT_USER_ID", "fallback-owner")
    from kroger_mcp.analytics.favorites import _resolve_user_id

    assert _resolve_user_id(None) == "fallback-owner"
    assert _resolve_user_id("explicit-id") == "explicit-id"


def test_mcp_user_id_prefers_per_invocation_env(monkeypatch):
    """Spec: KROGER_MCP_USER_ID wins over KROGER_MCP_DEFAULT_USER_ID."""
    from kroger_mcp.auth.dependencies import mcp_user_id

    monkeypatch.setenv("KROGER_MCP_DEFAULT_USER_ID", "default-owner")
    monkeypatch.setenv("KROGER_MCP_USER_ID", "profile-owner")
    assert mcp_user_id() == "profile-owner"


def test_mcp_user_id_falls_back_to_default(monkeypatch):
    """Spec: mcp_user_id falls back to KROGER_MCP_DEFAULT_USER_ID when no per-invocation override."""
    from kroger_mcp.auth.dependencies import mcp_user_id

    monkeypatch.delenv("KROGER_MCP_USER_ID", raising=False)
    monkeypatch.setenv("KROGER_MCP_DEFAULT_USER_ID", "default-owner")
    assert mcp_user_id() == "default-owner"


class TestPantryScoping:
    """Pantry operations must enforce user_id boundaries."""

    def test_user_a_cannot_see_user_b_pantry_items(self, two_users):
        from kroger_mcp.analytics import pantry

        a_id, b_id = two_users
        pantry.add_to_pantry(
            product_id="__E2E__pantry-isolated",
            description="A's milk",
            level=100,
            user_id=a_id,
        )

        b_items = pantry.get_pantry_status(user_id=b_id)
        b_product_ids = [item["product_id"] for item in b_items]
        assert "__E2E__pantry-isolated" not in b_product_ids


class TestMealPlanScoping:
    """Meal plan operations must enforce user_id boundaries."""

    def test_user_a_meal_plan_invisible_to_user_b(self, two_users):
        from kroger_mcp.analytics import meal_planning

        a_id, b_id = two_users
        meal_planning.create_meal_plan(
            name="__E2E__a-plan",
            start_date="2026-01-01",
            end_date="2026-01-07",
            user_id=a_id,
        )

        b_plans = meal_planning.list_meal_plans(user_id=b_id) if hasattr(
            meal_planning, "list_meal_plans"
        ) else meal_planning.get_meal_plans(user_id=b_id)
        b_names = [p.get("name") for p in (b_plans if isinstance(b_plans, list) else b_plans.get("plans", []))]
        assert "__E2E__a-plan" not in b_names


class TestSafetyScoping:
    """Safety operations must enforce user_id boundaries."""

    def test_safe_product_added_by_a_invisible_to_b(self, two_users):
        from kroger_mcp.analytics import safety

        a_id, b_id = two_users
        safety.add_to_safe_list(
            product_id="__E2E__safety-isolated",
            description="A's safe item",
            user_id=a_id,
        )

        b_safe = safety.get_safe_products(user_id=b_id)
        b_pids = [p["product_id"] for p in b_safe]
        assert "__E2E__safety-isolated" not in b_pids

    def test_new_user_gets_default_safety_settings(self, two_users):
        from kroger_mcp.analytics import safety

        _, b_id = two_users
        settings = safety.get_safety_settings(user_id=b_id)
        assert settings.get("filtering_enabled") in (1, True, "1")
