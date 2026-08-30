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

- **`test_manual_source_items.py`** — specs for the *unlinked* half of the same
  idea: an ingredient with no `product_id` at all, because the user buys it at
  Walmart. Manual status is derived from the missing id rather than declared by
  a flag, and `source` names the vendor so the list groups into per-vendor
  errand sections (`manual_purchase_by_source`).

  The `cart_gate` tests are the load-bearing ones. `check_cart_items_safety` is
  the single gate every cart-write path shares, and it used to recognize manual
  items by the `manual:<uuid>` prefix alone — but `is_manual_product_id(None)`
  is `False`, so making `product_id` optional would have let an unlinked item
  through to be POSTed to Kroger as `{"upc": None}`. Those tests run with the
  safety filter **off** and `confirm_unsafe=True`: a block that survives the
  most permissive configuration is what proves the invariant is structural
  rather than a tunable preference. They also cover the missing-key shape,
  which the old `item["product_id"]` subscript raised `KeyError` on.

  The rest pins the storage/display split that bit during implementation:
  `UNSPECIFIED_SOURCE` (`"Manual"`) is a display label, so writes go through
  `stored_source()` and persist `NULL`. Storing the sentinel made "never said
  where" indistinguishable from a store by that name and overwrote the
  `override_reason` note that older manual favorites are the sole carrier of —
  caught by `test_favorites_manual_items.py`, above.

## Isolation pattern

Fixtures that touch the database use a `tmp_path`-backed SQLite file via
`monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "<name>.db"))` before
calling `initialize_database()` / `ensure_initialized()`, so tests never
read or write the real dev/prod database.
