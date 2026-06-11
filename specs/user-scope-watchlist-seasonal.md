# Spec — User-scope deal_watchlist + seasonal_patterns

## Context
`deal_watchlist` and `seasonal_patterns` are the last two analytics tables that
are **global** in the SQLite schema and all application code (`product_id UNIQUE`,
`ON CONFLICT(product_id[, month])`, no `user_id` ever written). Every user shares
one watchlist and one set of seasonal patterns — the same class of multi-user bug
as the shared-Kroger-token bug already fixed. The PG schema (`pg_database.py`)
already declares both as user-scoped (`user_id UUID NOT NULL`,
`UNIQUE(user_id, product_id[, month])`), so on Postgres these two features **fail**
(NOT NULL violation + wrong conflict target). User chose to user-scope BOTH
(matches the rest of the multi-tenant remodel) over the minimal keep-global parity.

## Scope (exactly the two tables + their access sites)
- `deal_watchlist`: WRITE `tools/deal_tools.py` add_to_watchlist; READ
  `tools/deal_tools.py` scan_watchlist.
- `seasonal_patterns`: WRITE `analytics/seasonal.py` update_seasonal_patterns;
  READ `analytics/seasonal.py` get_upcoming_seasonal_items. Callers
  (`analytics/migration.py`, `analytics/predictions.py`) pass no user → resolve
  via the established `mcp_user_id()` fallback.

Owner resolution reuses the existing pattern: `user_id` arg, else `mcp_user_id()`
(honors `KROGER_MCP_USER_ID`, then `KROGER_MCP_DEFAULT_USER_ID`).

## Out of scope (flagged, deferred)
`purchase_events`/`orders` still write `user_id = NULL` (global). `update_seasonal_patterns`
computes from `purchase_events`, so its internal events read **stays global** here
(filtering by user against NULL-user rows would return empty → regress the feature).
Patterns are computed from global events but **stored/read per-owner** — correct
schema shape now, truly per-user once `purchase_events` ownership lands (separate task).

## Changes
1. **SQLite schema (`analytics/database.py initialize_database`)** — fresh DDL:
   - `deal_watchlist`: `product_id TEXT UNIQUE NOT NULL` → `product_id TEXT NOT NULL`,
     add `user_id TEXT`, add `UNIQUE(user_id, product_id)`.
   - `seasonal_patterns`: add `user_id TEXT`, `UNIQUE(product_id, month)` →
     `UNIQUE(user_id, product_id, month)`.
2. **SQLite migration (`run_schema_migrations`)** — existing DBs: SQLite cannot drop
   a column-level UNIQUE in place, so rebuild each table (rename→create→copy
   intersection of columns→drop) backfilling `user_id = mcp_user_id()`. Idempotent:
   only rebuilds when `user_id` column is absent. New shared helper
   `_rebuild_table_add_user_id(conn, table, new_ddl, owner)`.
3. **Writes** add `user_id` + `ON CONFLICT(user_id, …)`.
4. **Reads** add `WHERE user_id = ?` (`get_upcoming_seasonal_items` qualifies `sp.user_id`).
5. **PG schema** — already correct; no change. The point of the port: these now run on PG.

## Acceptance criteria (testable)
- AC-1 Two distinct users can watch the SAME product independently; each
  `scan_watchlist`/watchlist read returns only the caller's rows. (SQLite + PG)
- AC-2 `update_seasonal_patterns(product_id, user_id=A)` and `=B` produce
  independent rows; `get_upcoming_seasonal_items(user_id=A)` returns only A's. (SQLite + PG)
- AC-3 Migration on a pre-existing global table: after `run_schema_migrations`,
  `user_id` exists, every prior row is backfilled to the default owner, and the
  composite UNIQUE now permits two users × same product. Re-running is a no-op.
- AC-4 No `ON CONFLICT(product_id)` / `ON CONFLICT(product_id, month)` left on these
  tables; both writes go through `user_id`.
- AC-5 Full SQLite suite green; `tests/test_pg_backend.py` exercises watchlist +
  seasonal on live PG; mypy 0; ruff 0.
