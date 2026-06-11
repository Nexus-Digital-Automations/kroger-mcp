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
