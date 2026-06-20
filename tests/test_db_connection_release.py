"""Regression tests: auth code must RETURN pooled Postgres connections, not close them.

Critical-path (auth). A pooled psycopg connection obtained from ``pool.getconn()``
must go back via ``pool.putconn()``. The earlier code called ``conn.close()`` on it
unconditionally, which drops the socket without freeing the pool slot — so each
uncached session validation leaked one connection until the pool was exhausted and
every request died with ``PoolTimeout`` (observed as a site-wide "Internal Server
Error"). These tests lock in the per-backend release contract for both auth modules.
"""

from unittest.mock import MagicMock

import pytest

from kroger_mcp.auth import kroger_tokens, sessions


@pytest.mark.parametrize("module", [sessions, kroger_tokens], ids=["sessions", "kroger_tokens"])
def test_postgres_release_returns_to_pool_not_close(module, monkeypatch):
    """PG backend: _release must putconn the connection and never close it."""
    pool = MagicMock()
    monkeypatch.setattr(
        "kroger_mcp.analytics.pg_database._get_pool", lambda: pool, raising=True
    )
    conn = MagicMock()

    module._release(conn, "postgresql")

    pool.putconn.assert_called_once_with(conn)
    conn.close.assert_not_called()
    # Rolled back before return so no half-open transaction rides back into the pool.
    conn.rollback.assert_called_once()


@pytest.mark.parametrize("module", [sessions, kroger_tokens], ids=["sessions", "kroger_tokens"])
def test_sqlite_release_closes_connection(module):
    """SQLite backend: _release closes the connection (no pool involved)."""
    conn = MagicMock()

    module._release(conn, "sqlite")

    conn.close.assert_called_once()


@pytest.mark.parametrize("module", [sessions, kroger_tokens], ids=["sessions", "kroger_tokens"])
def test_postgres_release_putconn_even_if_rollback_fails(module, monkeypatch):
    """A rollback error must not prevent the slot from being returned to the pool."""
    pool = MagicMock()
    monkeypatch.setattr(
        "kroger_mcp.analytics.pg_database._get_pool", lambda: pool, raising=True
    )
    conn = MagicMock()
    conn.rollback.side_effect = RuntimeError("connection broken")

    module._release(conn, "postgresql")

    pool.putconn.assert_called_once_with(conn)
