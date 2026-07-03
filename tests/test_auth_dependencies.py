"""Tests for auth/dependencies.py's user_id resolution, including the
web-request ContextVar that lets chatbot-triggered tool calls run as the
actual logged-in user instead of the single-user MCP env-var default.
"""

from __future__ import annotations

import pytest

from kroger_mcp.auth import dependencies as deps


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KROGER_MCP_USER_ID", raising=False)
    monkeypatch.delenv("KROGER_MCP_DEFAULT_USER_ID", raising=False)


def test_web_user_id_takes_priority_over_env_vars(monkeypatch):
    monkeypatch.setenv("KROGER_MCP_USER_ID", "env-user")
    token = deps.set_web_user_id("web-user")
    try:
        assert deps.mcp_user_id() == "web-user"
    finally:
        deps.reset_web_user_id(token)


def test_falls_back_to_explicit_env_var_when_no_web_user(monkeypatch):
    monkeypatch.setenv("KROGER_MCP_USER_ID", "env-user")
    assert deps.mcp_user_id() == "env-user"


def test_falls_back_to_default_user_id_when_nothing_else_set(monkeypatch):
    monkeypatch.setenv("KROGER_MCP_DEFAULT_USER_ID", "default-user")
    assert deps.mcp_user_id() == "default-user"


def test_reset_restores_prior_value():
    outer_token = deps.set_web_user_id("outer")
    try:
        inner_token = deps.set_web_user_id("inner")
        assert deps.mcp_user_id() == "inner"
        deps.reset_web_user_id(inner_token)
        assert deps.mcp_user_id() == "outer"
    finally:
        deps.reset_web_user_id(outer_token)


def test_raises_when_nothing_resolves():
    with pytest.raises(RuntimeError):
        deps.mcp_user_id()
