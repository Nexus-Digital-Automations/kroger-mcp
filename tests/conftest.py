"""
Test bootstrap: synthesize the multi-tenant default user before any
analytics code runs.

In production, KROGER_MCP_DEFAULT_USER_ID is installed by
`migrate_to_multi_tenant.py` (which requires real credentials). Tests
just need a stable UUID for ownership; we set one here so add_to_pantry,
consume_from_pantry, etc. don't blow up at _resolve_user_id().
"""

import os
import uuid

import pytest

os.environ.setdefault("KROGER_MCP_DEFAULT_USER_ID", str(uuid.uuid5(uuid.NAMESPACE_DNS, "smart-shopper.tests")))

# Re-exported so existing imports keep working; canonical home is tests/_pg_support.py.
from _pg_support import RUNNING_ON_PG  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch):
    """Strip GEMINI_API_KEY so no test can reach the live Gemma endpoint.

    generate_draft() tries a Gemma selection on every call; without a key the
    client returns an error dict before any network I/O, and the draft falls
    back to rotation. Gemma-path tests monkeypatch
    draft_selection._chat_completion instead of setting a key.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_database_url():
    """Restore DATABASE_URL around every test.

    The backend-aware connection shim makes DATABASE_URL globally significant —
    it selects SQLite vs Postgres for get_db_connection(). A PG-backend test that
    sets DATABASE_URL must not leak the Postgres backend into the (SQLite-default)
    tests that follow, or they'd route to the PG pool and stall. Snapshot before,
    restore after, and clear the one-time init flag so each test re-initializes
    against its own backend.
    """
    prior = os.environ.get("DATABASE_URL")
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior
        try:
            import kroger_mcp.analytics.database as _db

            _db._initialized = False
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _pg_isolation_between_tests():
    """Give each test a fresh database when the suite runs on Postgres.

    The SQLite suite isolates tests by monkeypatching ``database.DB_FILE`` to a
    per-test tmp file. Under ``DATABASE_URL=postgres`` the backend ignores
    ``DB_FILE``, so every test in a file would share one database and contaminate
    the next (this is what produced the per-file errors when AC-2 was first run).
    Restore fresh-DB semantics: ensure the schema+seed exist, TRUNCATE every data
    table, then re-seed — before each test.

    Inert on the default SQLite path (no ``DATABASE_URL`` → immediate yield), so
    the SQLite suite is untouched.
    """
    if not os.environ.get("DATABASE_URL"):
        yield
        return

    import kroger_mcp.analytics.database as _db
    from kroger_mcp.analytics import pg_database

    # Self-managing tests (test_etl_sqlite_to_pg, test_pg_backend) create and DROP
    # their own throwaway databases and flip DATABASE_URL. The connection pool is a
    # module-level singleton bound to whatever DSN it first saw, so after those tests
    # it points at a dropped DB and poisons every later test (even DB-less ones).
    # Drop it here so this test rebuilds the pool against the CURRENT DATABASE_URL.
    pg_database.close_pool()

    # initialize_database() both creates tables and seeds reference rows
    # (favorite_lists, safety_settings); run it, wipe, then re-seed so each test
    # starts from the clean-with-seeds state a fresh SQLite file would give.
    _db._initialized = False
    _db.ensure_initialized()
    with _db.get_db_cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = [row[0] for row in cursor.fetchall()]
        if tables:
            quoted = ", ".join('"' + t + '"' for t in tables)
            cursor.execute("TRUNCATE " + quoted + " RESTART IDENTITY CASCADE")
    _db._initialized = False
    _db.ensure_initialized()

    # Seed the default test user. Postgres enforces the user_id → users.id FKs
    # that the SQLite test path silently tolerates, so user-scoped writes need a
    # real users row for KROGER_MCP_DEFAULT_USER_ID (the ownership UUID set above).
    default_user = os.environ.get("KROGER_MCP_DEFAULT_USER_ID")
    if default_user:
        with _db.get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (id, email, password_hash, display_name) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
                (default_user, "tests@smart-shopper.local", "x", "Test User"),
            )
    yield
