"""Critical-path: per-user Kroger token isolation + ciphertext at rest.

This guards the multi-user correctness bug: with two logged-in users, one user's
Kroger OAuth token must never overwrite or leak into the other's. It also asserts
tokens are encrypted at rest (the stored value is not the plaintext access token)
and that ``get_authenticated_client(user_A)`` can never surface user B's token.

Hermetic: a temp SQLite DB + an explicit Fernet master key via
``KROGER_TOKEN_MASTER_KEY`` — no Keychain, no network, no shared on-disk file.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest

# A fixed, valid Fernet key so encrypt/decrypt is deterministic and hermetic.
_TEST_MASTER_KEY = "0RRZjBKujXWGCaS0ijh0QRh9yMT0tUi0ymgoKxVb1FM="

_USER_A = "11111111-1111-1111-1111-111111111111"
_USER_B = "22222222-2222-2222-2222-222222222222"

_TOKEN_A = {
    "access_token": "ACCESS_TOKEN_FOR_USER_A_secret_value",
    "token_type": "bearer",
    "refresh_token": "REFRESH_TOKEN_FOR_USER_A",
    "expires_in": 1800,
}
_TOKEN_B = {
    "access_token": "ACCESS_TOKEN_FOR_USER_B_secret_value",
    "token_type": "bearer",
    "refresh_token": "REFRESH_TOKEN_FOR_USER_B",
    "expires_in": 1800,
}


@pytest.fixture()
def kroger_tokens_mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator:
    """Point token storage at a temp SQLite DB with a known master key.

    Reimports crypto + token modules so the cached Fernet/connection helpers
    pick up the test env, and yields a freshly-imported ``kroger_tokens``.
    """
    db_file = str(tmp_path / "auth_test.db")
    monkeypatch.setenv("KROGER_TOKEN_MASTER_KEY", _TEST_MASTER_KEY)
    monkeypatch.delenv("DATABASE_URL", raising=False)  # force SQLite backend

    from kroger_mcp.analytics import database

    monkeypatch.setattr(database, "DB_FILE", db_file)

    # Reset the per-process Fernet cache so it rebuilds from the test key.
    from kroger_mcp.auth import token_crypto

    monkeypatch.setattr(token_crypto, "_fernet", None)

    # Create the auth tables (users, kroger_tokens, ...) in the temp DB.
    from kroger_mcp.analytics import pg_database

    pg_database.initialize_sqlite_auth_tables()

    # Seed the two users — kroger_tokens.user_id FK-references users(id).
    conn = database.get_db_connection()
    try:
        conn.executemany(
            "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
            [
                (_USER_A, "a@example.com", "x", "User A"),
                (_USER_B, "b@example.com", "x", "User B"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    import kroger_mcp.auth.kroger_tokens as kroger_tokens

    kroger_tokens = importlib.reload(kroger_tokens)
    yield kroger_tokens


def test_tokens_are_isolated_per_user(kroger_tokens_mod) -> None:
    """A and B store distinct tokens; loading one never returns the other's."""
    kroger_tokens_mod.save_kroger_token(_USER_A, _TOKEN_A)
    kroger_tokens_mod.save_kroger_token(_USER_B, _TOKEN_B)

    loaded_a = kroger_tokens_mod.load_kroger_token(_USER_A)
    loaded_b = kroger_tokens_mod.load_kroger_token(_USER_B)

    assert loaded_a is not None and loaded_b is not None
    assert loaded_a["access_token"] == _TOKEN_A["access_token"]
    assert loaded_b["access_token"] == _TOKEN_B["access_token"]
    # The decisive isolation assertion: A never surfaces B's secret.
    assert loaded_a["access_token"] != _TOKEN_B["access_token"]
    assert loaded_a["refresh_token"] != _TOKEN_B["refresh_token"]


def test_one_user_overwrite_does_not_touch_the_other(kroger_tokens_mod) -> None:
    """Re-saving A's token (the old bug's overwrite) leaves B intact."""
    kroger_tokens_mod.save_kroger_token(_USER_A, _TOKEN_A)
    kroger_tokens_mod.save_kroger_token(_USER_B, _TOKEN_B)

    # Simulate A re-authenticating / refreshing.
    rotated_a = dict(_TOKEN_A, access_token="ROTATED_ACCESS_TOKEN_FOR_USER_A")
    kroger_tokens_mod.save_kroger_token(_USER_A, rotated_a)

    assert (
        kroger_tokens_mod.load_kroger_token(_USER_A)["access_token"]
        == "ROTATED_ACCESS_TOKEN_FOR_USER_A"
    )
    # B is untouched — the whole point of the fix.
    assert kroger_tokens_mod.load_kroger_token(_USER_B)["access_token"] == _TOKEN_B["access_token"]


def test_token_is_ciphertext_at_rest(kroger_tokens_mod) -> None:
    """The raw DB value is Fernet ciphertext, not the plaintext access token."""
    kroger_tokens_mod.save_kroger_token(_USER_A, _TOKEN_A)

    from kroger_mcp.analytics import database

    conn = database.get_db_connection()
    try:
        row = conn.execute(
            "SELECT access_token, refresh_token FROM kroger_tokens WHERE user_id = ?",
            (_USER_A,),
        ).fetchone()
    finally:
        conn.close()

    stored_access, stored_refresh = row[0], row[1]
    assert stored_access != _TOKEN_A["access_token"]
    assert _TOKEN_A["access_token"] not in stored_access
    assert stored_refresh != _TOKEN_A["refresh_token"]
    assert _TOKEN_A["refresh_token"] not in stored_refresh
    # Fernet ciphertext is url-safe base64 beginning with the version byte 'gA'.
    assert stored_access.startswith("gA")


def test_get_authenticated_client_loads_only_the_requested_user(
    kroger_tokens_mod, monkeypatch
) -> None:
    """get_authenticated_client(A) reads A's row, never B's — no shared global."""
    kroger_tokens_mod.save_kroger_token(_USER_A, _TOKEN_A)
    kroger_tokens_mod.save_kroger_token(_USER_B, _TOKEN_B)

    from kroger_mcp.tools import shared

    # Avoid real Kroger network calls: supply credentials and stub the probe.
    monkeypatch.setattr(
        shared,
        "get_kroger_credentials",
        lambda user_id=None: {"client_id": "c", "client_secret": "s", "redirect_uri": "r"},
    )

    captured: dict[str, object] = {}

    class _FakeClientInner:
        def __init__(self) -> None:
            self.token_info: dict | None = None
            self.token_file = None

    class _FakeKrogerAPI:
        def __init__(self, client_id=None, client_secret=None, redirect_uri=None) -> None:
            self.client = _FakeClientInner()

        def test_current_token(self) -> bool:
            # Record which token this fresh client was handed.
            captured["access_token"] = self.client.token_info["access_token"]
            return True

    monkeypatch.setattr(shared, "KrogerAPI", _FakeKrogerAPI)

    client = shared.get_authenticated_client(_USER_A)
    assert client.client.token_info["access_token"] == _TOKEN_A["access_token"]
    assert captured["access_token"] == _TOKEN_A["access_token"]
    # Decisive: requesting A must never surface B's secret.
    assert captured["access_token"] != _TOKEN_B["access_token"]
    # And token_file stays None so refreshes can't leak to the shared file.
    assert client.client.token_file is None


def test_missing_token_raises_authentication_required(kroger_tokens_mod, monkeypatch) -> None:
    """A user with no stored token gets the explicit re-auth signal."""
    from kroger_mcp.tools import shared

    # Credentials present, but no stored token for this user → re-auth signal.
    monkeypatch.setattr(
        shared,
        "get_kroger_credentials",
        lambda user_id=None: {"client_id": "c", "client_secret": "s", "redirect_uri": "r"},
    )

    with pytest.raises(Exception, match="Authentication required"):
        shared.get_authenticated_client(_USER_A)
