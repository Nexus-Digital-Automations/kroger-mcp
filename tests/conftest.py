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

os.environ.setdefault("KROGER_MCP_DEFAULT_USER_ID", str(uuid.uuid5(uuid.NAMESPACE_DNS, "smart-shopper.tests")))
