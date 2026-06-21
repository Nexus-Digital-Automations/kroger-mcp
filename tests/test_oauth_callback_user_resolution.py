"""Regression tests: the Kroger OAuth /callback resolves the user from the cookie.

Critical-path (auth / token persistence). /callback is a PUBLIC_PATH, so
AuthMiddleware leaves request.state.user None and never validates the session
cookie. The handler must therefore read+validate the SameSite=Lax session cookie
itself — otherwise current_user_id(request) 401s and the OAuth round-trip dies at
the callback with the auth code in hand (the original "authentication required"
bug, with zero tokens ever persisted). These tests pin that behavior.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from kroger_mcp.auth.middleware import SESSION_COOKIE
from kroger_mcp.web.routes import settings as settings_route

USER_ID = "11111111-1111-1111-1111-111111111111"
STATE = "teststate123"


def _request(cookies: dict) -> SimpleNamespace:
    return SimpleNamespace(cookies=cookies)


@pytest.fixture
def oauth_state_file(tmp_path, monkeypatch):
    """Point the handler's PKCE state file at a tmp file seeded for our state."""
    state_file = tmp_path / "oauth_state.json"
    state_file.write_text(
        json.dumps(
            {
                "state": STATE,
                "pkce_params": {"code_verifier": "verifier"},
                "redirect_uri": "https://prod.example/callback",
            }
        )
    )
    monkeypatch.setattr(settings_route, "_WEB_OAUTH_STATE_FILE", state_file)
    return state_file


@pytest.fixture
def stub_token_exchange(monkeypatch):
    """Stub the Kroger client + credential/token helpers the handler imports."""
    monkeypatch.setattr(
        "kroger_mcp.tools.shared.get_kroger_credentials",
        lambda user_id=None: {"client_id": "c", "client_secret": "s", "redirect_uri": "r"},
    )

    saved = {}
    monkeypatch.setattr(
        "kroger_mcp.auth.kroger_tokens.save_kroger_token",
        lambda user_id, token_info: saved.update({"user_id": user_id, "token": token_info}),
    )
    # The callback must NOT delete the token it just saved. Track deletes so the
    # regression (a stray invalidate_authenticated_client wiping the fresh token,
    # leaving kroger_tokens empty under a "success" banner) is caught.
    monkeypatch.setattr(
        "kroger_mcp.auth.kroger_tokens.delete_kroger_token",
        lambda user_id: saved.update({"deleted": user_id}),
    )

    class _FakeClient:
        def get_token_with_authorization_code(self, code, code_verifier):
            return {"access_token": "tok", "refresh_token": "r", "expires_in": 1800}

    class _FakeKrogerAPI:
        def __init__(self, **kwargs):
            self.client = _FakeClient()

    monkeypatch.setattr("kroger_api.KrogerAPI", _FakeKrogerAPI)
    return saved


def test_callback_resolves_user_from_cookie_and_saves_token(
    oauth_state_file, stub_token_exchange, monkeypatch
):
    """Valid session cookie → user resolved, token persisted, oauth=success."""
    monkeypatch.setattr(
        "kroger_mcp.auth.sessions.validate_session",
        lambda token: {"id": USER_ID} if token == "good-token" else None,
    )

    resp = asyncio.run(
        settings_route.oauth_callback(
            _request({SESSION_COOKIE: "good-token"}), code="authcode", state=STATE, error=None
        )
    )

    assert resp.status_code in (302, 307)
    assert "oauth=success" in resp.headers["location"]
    # The token was scoped to the cookie's user — not a 401.
    assert stub_token_exchange["user_id"] == USER_ID
    # The freshly saved token must survive — the callback must not delete it.
    assert "deleted" not in stub_token_exchange
    # State file consumed on success.
    assert not oauth_state_file.exists()


def test_callback_without_session_cookie_redirects_not_logged_in(oauth_state_file, monkeypatch):
    """No session cookie → not_logged_in redirect, never a 401, no token saved."""
    monkeypatch.setattr(
        "kroger_mcp.auth.sessions.validate_session", lambda token: None
    )
    called = {"save": False}
    monkeypatch.setattr(
        "kroger_mcp.auth.kroger_tokens.save_kroger_token",
        lambda *a, **k: called.update(save=True),
    )

    resp = asyncio.run(
        settings_route.oauth_callback(_request({}), code="authcode", state=STATE, error=None)
    )

    assert resp.status_code in (302, 307)
    assert "detail=not_logged_in" in resp.headers["location"]
    assert called["save"] is False


def test_callback_with_invalid_cookie_redirects_not_logged_in(oauth_state_file, monkeypatch):
    """Present-but-invalid session cookie → not_logged_in, not a crash/401."""
    monkeypatch.setattr(
        "kroger_mcp.auth.sessions.validate_session", lambda token: None
    )

    resp = asyncio.run(
        settings_route.oauth_callback(
            _request({SESSION_COOKIE: "expired"}), code="authcode", state=STATE, error=None
        )
    )

    assert resp.status_code in (302, 307)
    assert "detail=not_logged_in" in resp.headers["location"]
