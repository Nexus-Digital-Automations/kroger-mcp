"""Regression tests: both OAuth authorization flows request the same scopes.

Critical-path (auth). Two code paths mint a Kroger authorization URL — the MCP
`auth(action='start')` tool and the web settings route. They previously each
hardcoded their own scope string, and both omitted `profile.compact`.

That omission broke two things at once:

1. `auth(action='get_profile')` reads /identity/profile, which requires
   `profile.compact` — so it returned 403 unconditionally, never transiently.
2. More subtly, kroger-api's ``test_token()`` validates ANY token by GETting
   /v1/connect/oauth2/profile. Without the scope that probe always 403s, so
   every token check fell through to a refresh round-trip and surfaced
   "Authentication required" whenever the refresh failed.

These tests pin the scope set and, critically, pin that the two flows share one
constant so they cannot drift apart again — a token minted by one path must
validate on the other.
"""

from kroger_mcp.tools.shared import KROGER_OAUTH_SCOPES

REQUIRED_SCOPES = {"product.compact", "cart.basic:write", "profile.compact"}


def test_scope_constant_contains_every_required_scope():
    assert set(KROGER_OAUTH_SCOPES.split()) == REQUIRED_SCOPES


def test_profile_scope_present_so_token_validation_can_succeed():
    """kroger-api's test_token() probes /v1/connect/oauth2/profile."""
    assert "profile.compact" in KROGER_OAUTH_SCOPES.split()


def test_mcp_auth_tool_uses_the_shared_constant():
    """The MCP start flow must not reintroduce its own hardcoded scope string."""
    from kroger_mcp.tools import auth_tools

    assert auth_tools.KROGER_OAUTH_SCOPES is KROGER_OAUTH_SCOPES

    source = (auth_tools.__file__ or "").replace(".pyc", ".py")
    with open(source) as handle:
        body = handle.read()
    assert '"product.compact cart.basic:write' not in body


def test_web_settings_route_uses_the_shared_constant():
    """The web flow must request the identical scope set as the MCP flow."""
    from kroger_mcp.web.routes.api import settings

    source = (settings.__file__ or "").replace(".pyc", ".py")
    with open(source) as handle:
        body = handle.read()
    assert "scope=KROGER_OAUTH_SCOPES" in body
    assert 'scope="product.compact cart.basic:write"' not in body
