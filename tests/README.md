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

- **`test_weekly_draft.py`** — specs for the passive weekly workflow's
  planning half: `week_start_for_date` under both Sunday (default 6) and
  Monday conventions, `generate_draft` (dinner-only slots spread evenly over
  the horizon, idempotent — a repeat call returns the same draft rather than
  re-rolling, clean error with zero saved recipes, `already_planned` when a
  real plan covers next week, rotation away from recently-cooked recipes),
  draft invisibility until `approve_draft`, and `next_week_needs_plan`
  anchoring on the configured week start while ignoring unapproved drafts.
  Also the opt-in `draft_auto_approve` setting: on, the generated plan is
  born live (`is_draft=0`, immediately reconcilable and visible) yet an
  already-existing draft is never retroactively approved; and the
  `draft_awaiting_approval` bell helper that points at the unapproved draft
  for next week. Data-integrity stakes: a wrong week boundary or a deducting
  draft silently drains the pantry for meals that were never approved.

- **`test_gemma_draft.py`** — specs for the Gemma-backed seasonal dinner
  selection layered onto `generate_draft`. A mocked Gemma success must be used
  exactly (the model's recipe ids in order, its one-line reasons persisted as
  `meal_entries.notes`, `selection_mode: "gemma"`), and — the load-bearing
  half — every failure mode (provider error dict, malformed JSON, unknown
  recipe ids, too few picks, raised exception, missing `GEMINI_API_KEY`) must
  silently fall back to the recency rotation and still create the draft. Also
  pins `parse_selection`'s fence-stripping/dedupe/truncation and the
  hermeticity guarantee: `tests/conftest.py` strips `GEMINI_API_KEY` so no
  test can reach the live endpoint.

- **`test_snack_log.py`** — specs for the one-call snack log
  (`favorites(action='log_snack')`): a matched item deducts a flat
  `SNACK_LOG_DEDUCT_PERCENT` exactly once and writes an auditable
  `snack_consumed` purchase event; an unmatched item never raises (locked
  product decision: silent) but is persisted to `unmatched_snack_log` and
  surfaced via `list_unmatched_snacks_for_bell`; and the consumption signal —
  `consumed_count_since_order` increments per log, resets on
  `mark_snacks_ordered`, and pre-ticks `check_snacks` exactly at
  `SNACK_CONSUMED_REORDER_THRESHOLD`.

## Isolation pattern

Fixtures that touch the database use a `tmp_path`-backed SQLite file via
`monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "<name>.db"))` before
calling `initialize_database()` / `ensure_initialized()`, so tests never
read or write the real dev/prod database.
