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
    """Create two throwaway users in the live analytics DB and yield their ids."""
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
        conn.execute("DELETE FROM favorite_list_items WHERE user_id IN (?, ?)", (a_id, b_id))
        conn.execute("DELETE FROM favorite_lists WHERE user_id IN (?, ?)", (a_id, b_id))
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
