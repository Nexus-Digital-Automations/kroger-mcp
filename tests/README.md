# Tests

Test suite for the Smart Shopper / Kroger MCP server, run via `pytest`.

## Regression tests worth calling out

- **`test_schema_migration_fk_safety.py`** — regression coverage for
  `run_schema_migrations()`'s foreign-key safety. `_rebuild_table_add_user_id`
  (in `src/kroger_mcp/analytics/database.py`) rebuilds formerly-global SQLite
  tables (e.g. `favorite_lists`) into a user-scoped shape via
  CREATE-new/copy/DROP-old/RENAME. Because SQLite's `DROP TABLE` performs an
  implicit `DELETE FROM` when foreign-key enforcement is on, an earlier
  version of this migration silently cascade-deleted rows in dependent
  tables (e.g. `favorite_list_items`) on any fresh/unmigrated install with
  real favorites. This file locks in the fix: foreign keys are disabled for
  the migration transaction and re-enabled after commit, and the
  post-rebuild integrity check is scoped to the rebuilt table's actual
  dependents rather than the whole database (so unrelated pre-existing FK
  debt in a long-lived dev/prod DB doesn't false-positive the migration).

- **`test_favorites_manual_items.py`** — specs for favorites items with no
  linked Kroger product (farmers-market finds, home-grown herbs). Because
  `favorite_list_items.product_id` is NOT NULL and half the composite primary
  key, such an item carries a synthetic `manual:<uuid>` id plus an `is_manual`
  flag; these tests pin that it stores, survives every read path (including
  the separate GROUP BY shape the aggregate `default` list uses), stays manual
  when moved between lists, and — the part that matters — is never sent to the
  Kroger cart while never being silently dropped either. The shopping-list
  case asserts after a `_save_shopping_list`/`_load_shopping_list` round-trip
  on purpose: `manual_purchase` was previously absent from the
  `user_shopping_lists` column list, so the flag was dropped on reload and
  recipe overrides silently reverted to ordinary unlinked rows.

## Isolation pattern

Fixtures that touch the database use a `tmp_path`-backed SQLite file via
`monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "<name>.db"))` before
calling `initialize_database()` / `ensure_initialized()`, so tests never
read or write the real dev/prod database.
