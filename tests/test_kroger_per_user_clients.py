"""Per-user Kroger credentials actually drive the API clients (hybrid model).

Critical path: a power user's own client_id must build their clients and refresh
their token — otherwise their token refreshes under the wrong app (session dies)
and their reads leak onto the shared rate bucket. These tests pin the wiring.
"""

from __future__ import annotations

from types import SimpleNamespace

import kroger_mcp.tools.shared as shared


class _FakeAuth:
    def get_token_with_client_credentials(self, scope):
        return {"access_token": "tok", "scope": scope}


class _FakeAPI:
    """Records the client_id it was built with; pretends its token is valid."""

    built: list[str | None] = []

    def __init__(self, client_id=None, client_secret=None, redirect_uri=None):
        type(self).built.append(client_id)
        self.client = SimpleNamespace(client_id=client_id, token_info=None)
        self.authorization = _FakeAuth()

    def test_current_token(self):
        return self.client.token_info is not None or True


def _patch_api(monkeypatch):
    _FakeAPI.built = []
    monkeypatch.setattr(shared, "KrogerAPI", _FakeAPI)
    monkeypatch.setattr(shared, "load_token", lambda _f: None)
    monkeypatch.setattr(shared, "_resolve_pref_user_id", lambda uid: uid)


def test_cc_client_cached_per_client_id_not_per_user(monkeypatch):
    _patch_api(monkeypatch)
    shared._cc_clients.clear()
    creds = {
        "u1": {"client_id": "APP_A", "client_secret": "sa", "redirect_uri": ""},
        "u2": {"client_id": "APP_B", "client_secret": "sb", "redirect_uri": ""},
        "u3": {"client_id": "APP_A", "client_secret": "sa", "redirect_uri": ""},
    }
    monkeypatch.setattr(shared, "get_kroger_credentials", lambda user_id=None: creds[user_id])

    shared.get_client_credentials_client("u1")  # builds APP_A
    shared.get_client_credentials_client("u2")  # builds APP_B
    shared.get_client_credentials_client("u3")  # APP_A already cached -> reuse

    assert _FakeAPI.built == ["APP_A", "APP_B"]
    assert set(shared._cc_clients) == {"APP_A", "APP_B"}


def test_missing_credentials_raises(monkeypatch):
    _patch_api(monkeypatch)
    shared._cc_clients.clear()
    monkeypatch.setattr(
        shared,
        "get_kroger_credentials",
        lambda user_id=None: {"client_id": "", "client_secret": "", "redirect_uri": ""},
    )
    import pytest

    with pytest.raises(Exception, match="not configured"):
        shared.get_client_credentials_client("nobody")


def test_authenticated_client_built_with_user_client_id(monkeypatch):
    _patch_api(monkeypatch)
    monkeypatch.setattr(
        shared,
        "get_kroger_credentials",
        lambda user_id=None: {
            "client_id": "POWER_USER_APP",
            "client_secret": "x",
            "redirect_uri": "https://app/callback",
        },
    )
    import kroger_mcp.auth.kroger_tokens as kt

    monkeypatch.setattr(
        kt, "load_kroger_token", lambda uid: {"access_token": "a", "refresh_token": "b"}
    )

    client = shared.get_authenticated_client("user-1")
    # The client the user's token will refresh against carries THEIR client_id.
    assert client.client.client_id == "POWER_USER_APP"
    assert _FakeAPI.built[-1] == "POWER_USER_APP"


def test_authenticated_client_requires_credentials(monkeypatch):
    _patch_api(monkeypatch)
    monkeypatch.setattr(
        shared,
        "get_kroger_credentials",
        lambda user_id=None: {"client_id": "", "client_secret": "", "redirect_uri": ""},
    )
    import pytest

    with pytest.raises(Exception, match="Authentication required"):
        shared.get_authenticated_client("user-1")


def test_invalidate_evicts_only_callers_app(monkeypatch):
    _patch_api(monkeypatch)
    shared._cc_clients.clear()
    shared._cc_clients["SHARED_ENV_APP"] = _FakeAPI(client_id="SHARED_ENV_APP")
    shared._cc_clients["POWER_APP"] = _FakeAPI(client_id="POWER_APP")
    monkeypatch.setattr(
        shared,
        "get_kroger_credentials",
        lambda user_id=None: {"client_id": "POWER_APP", "client_secret": "s", "redirect_uri": ""},
    )

    shared.invalidate_client_credentials_client("power-user")
    # Only the caller's app evicted; everyone else on the shared app untouched.
    assert "POWER_APP" not in shared._cc_clients
    assert "SHARED_ENV_APP" in shared._cc_clients
