"""
Unit tests for default servings preference functionality.
"""

import pytest

from kroger_mcp.analytics.database import get_db_connection
from kroger_mcp.auth.dependencies import default_user_id
from kroger_mcp.tools.shared import get_default_servings, set_default_servings


@pytest.fixture(autouse=True)
def cleanup_preferences():
    """Clear this user's stored default_servings_per_meal between tests."""

    def _clear():
        conn = get_db_connection()
        try:
            conn.execute(
                "DELETE FROM user_settings WHERE user_id = ? AND setting_key = ?",
                (default_user_id(), "default_servings_per_meal"),
            )
            conn.commit()
        finally:
            conn.close()

    _clear()
    yield
    _clear()


def test_get_default_servings_returns_4_by_default():
    """Test that default servings is 4 if not set."""
    servings = get_default_servings(user_id=default_user_id())
    assert servings == 4


def test_set_and_get_default_servings():
    """Test setting and retrieving default servings."""
    set_default_servings(2, user_id=default_user_id())
    assert get_default_servings(user_id=default_user_id()) == 2

    set_default_servings(6, user_id=default_user_id())
    assert get_default_servings(user_id=default_user_id()) == 6

    set_default_servings(1, user_id=default_user_id())
    assert get_default_servings(user_id=default_user_id()) == 1


def test_set_default_servings_validation():
    """Test servings validation (must be 1-20)."""
    with pytest.raises(ValueError, match="Servings must be between 1 and 20"):
        set_default_servings(0, user_id=default_user_id())  # Too low

    with pytest.raises(ValueError, match="Servings must be between 1 and 20"):
        set_default_servings(21, user_id=default_user_id())  # Too high

    # Edge cases should work
    set_default_servings(1, user_id=default_user_id())  # Min
    assert get_default_servings(user_id=default_user_id()) == 1

    set_default_servings(20, user_id=default_user_id())  # Max
    assert get_default_servings(user_id=default_user_id()) == 20


def _read_setting(key: str):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT setting_value FROM user_settings WHERE user_id = ? AND setting_key = ?",
            (default_user_id(), key),
        ).fetchone()
        return row["setting_value"] if row else None
    finally:
        conn.close()


def _write_setting(key: str, value: str):
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, setting_key, setting_value)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """,
            (default_user_id(), key, value),
        )
        conn.commit()
    finally:
        conn.close()


def test_default_servings_persists():
    """Spec: setting persists to user_settings row and round-trips through get_default_servings."""
    set_default_servings(3, user_id=default_user_id())
    assert _read_setting("default_servings_per_meal") == "3"
    assert get_default_servings(user_id=default_user_id()) == 3


def test_default_servings_does_not_affect_other_preferences():
    """Spec: writing default_servings does not clobber other rows in user_settings."""
    _write_setting("preferred_location_id", "12345")

    set_default_servings(2, user_id=default_user_id())

    assert _read_setting("default_servings_per_meal") == "2"
    assert _read_setting("preferred_location_id") == "12345"

    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM user_settings WHERE user_id = ? AND setting_key = ?",
            (default_user_id(), "preferred_location_id"),
        )
        conn.commit()
    finally:
        conn.close()
