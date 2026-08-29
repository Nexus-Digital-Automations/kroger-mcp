# Favorites: items without a linked Kroger product

## Context

A favorites list today can only hold real Kroger products. `favorite_list_items.product_id`
is `NOT NULL` **and** half the composite primary key `(list_id, product_id)` in both backends
(`analytics/database.py:453`, `analytics/pg_database.py:262`), and every write path validates
it (`favorites_tools.py:306`, `analytics/favorites.py:550`, `web/routes/api/favorites.py:26`).

Recipes already solved the analogous problem with `override: true` + `override_reason`, but
they store to a JSON file (`kroger_recipes.json`) with no schema to fight, so that fix was
pure Python validation. Favorites needs a storage answer.

Downstream, the shopping list already has a first-class `manual_purchase: True` item flag
(`_commit_recipe_items` writes `product_id: None` + `manual_purchase: True`;
`shopping_list_to_cart` and `_shopping_list_impl` both split those out of the Kroger cart
add). Favorites' manual items should feed that same channel rather than invent a new one.

## Design

**Storage — sentinel id, not a nullable column.** A manual item gets a synthetic
`product_id` of `manual:<uuid4hex>` (39 chars, fits PG's `VARCHAR(50)`), plus two new
columns: `is_manual` (SQLite `INTEGER DEFAULT 0` / PG `BOOLEAN DEFAULT FALSE`) and
`override_reason` (`TEXT`). The primary key, every existing query, every
`/items/{product_id}` route, and the whole frontend keep working untouched. Joins against
`pantry_items` / `product_statistics` are all `LEFT JOIN`s that simply find no match.

`is_manual` is declared `BOOLEAN` in PG, so it must be added to `database._PG_BOOL_COLS` —
`test_pg_bool_literal_translation.py` asserts that allow-list covers every PG BOOLEAN column.
Rows read back as `int` on SQLite and `bool` on PG, so the Python layer coerces with `bool()`.

**Manual-ness is inferred two ways.** The `is_manual` column is authoritative, but
`add_to_list`/`bulk_add_to_list` also treat a `manual:`-prefixed `product_id` as manual. That
backstop matters for the existing "Move to List" action, which re-POSTs an item's stored
`product_id` to another list — without it, moving a manual item would silently create a
product-linked row pointing at a fake UPC.

**Downstream: surface, never silently drop.** The `order` action splits manual items into a
`manual_purchase` list (excluded from the Kroger cart add, shown in the preview and the final
response), mirroring `recipes(action='preview_order')`. Both shopping-list push paths
(`add-to-shopping-list` for a whole list, `snacks/add-to-list` for ticked snacks) write manual
items as `product_id: None` + `manual_purchase: True` — the existing shopping-list convention —
so the sentinel id never leaks out of the favorites tables. The row action-menu hides
`+ Cart` and `Add to Recipe…` for manual items, since both call product-id endpoints.

**Persistence gap found on the way (fixed).** `user_shopping_lists` had no `manual_purchase`
or `notes` column, and `_save_shopping_list` writes a fixed column list — so the flag was
dropped on the next `_load_shopping_list`. Both consumers read it off the *loaded* list
(`shopping_list_to_cart:580`, `_shopping_list_impl:621`), so a manual item would silently
revert to an ordinary unlinked row after a reload. This already broke recipe overrides; both
columns are now persisted in both backends, with idempotent migrations.

**Analytics: manual stock levels only.** `set_stock_level` / `update_quantity` / `get_low_stock`
work unchanged (those columns live on `favorite_list_items` itself). `suggest` is
purchase-history-driven and never matches a sentinel id, so manual items are naturally absent.

## Decisions

- [x] Manual items are stored in `favorite_list_items` with a synthetic `manual:<uuid4hex>`
      product_id plus `is_manual` / `override_reason` columns — the composite primary key
      `(list_id, product_id)` is NOT changed and `product_id` stays NOT NULL
  verify: present "manual:" src/kroger_mcp/analytics/favorites.py
- [x] `override_reason` is optional, not required — unlike recipes' `override_reason`, a
      manual favorite may be added with no justification string
  verify: tests tests/test_favorites_manual_items.py::test_manual_item_without_reason_is_accepted
- [x] `is_manual` is registered in `database._PG_BOOL_COLS` so a future `is_manual = 1`
      literal is translated for Postgres
  verify: present "is_manual" src/kroger_mcp/analytics/database.py
- [x] The `order` action surfaces manual items as a `manual_purchase` list in both the
      preview and the executed response, and never sends them to the Kroger cart
  verify: tests tests/test_favorites_manual_items.py::test_order_preview_splits_manual_items
- [x] Both shopping-list push paths write manual favorites as `product_id: None` +
      `manual_purchase: True`, matching the recipe-override convention already in
      `_commit_recipe_items`
  verify: tests tests/test_favorites_manual_items.py::test_add_to_shopping_list_marks_manual_items
- [x] `user_shopping_lists` persists `manual_purchase` and `notes`, so the flag survives a
      save/load round-trip instead of being dropped by `_save_shopping_list`'s fixed column
      list — a pre-existing bug that also silently broke recipe overrides
  verify: present "manual_purchase" src/kroger_mcp/tools/shopping_list_tools.py
- [x] The "never carted" invariant is enforced server-side, not only in the browser: all three
      caller-facing cart-add entry points (`web/routes/api/products.py::add_product_to_cart`,
      `web/routes/api/cart.py::add_to_cart`, and the `cart` MCP tool's add action) reject a
      `manual:` id before any Kroger API call, and batch mode fails before ordering any item.
      `_add_item_to_local_cart` — the single writer every cart-add path funnels through —
      raises as a backstop, so a path that loses track of a manual item fails loudly
  verify: tests tests/test_favorites_manual_items.py::test_cart_add_rejects_a_manual_id

## Acceptance Criteria

- [x] `favorites(action='add_item', list_id=..., description='sourdough starter', manual=True)`
      succeeds with no `product_id` and stores a `manual:`-prefixed row
  verify: tests tests/test_favorites_manual_items.py::test_add_item_without_product_id
- [x] A single `add_item` with neither `product_id` nor `manual=True` still fails with the
      existing validation error (no silent acceptance of an unlinked item)
  verify: tests tests/test_favorites_manual_items.py::test_add_item_still_requires_product_or_manual
- [x] Bulk `add_item` accepts `{"description": ..., "manual": true}` entries alongside
      product-linked ones
  verify: tests tests/test_favorites_manual_items.py::test_bulk_add_mixes_manual_and_linked
- [x] `get_items` returns `is_manual` and `override_reason` on every item, on both the
      per-list and the aggregated default-list query paths
  verify: tests tests/test_favorites_manual_items.py::test_get_items_exposes_manual_fields
- [x] Moving a manual item to another list via the API keeps it manual (prefix backstop)
  verify: tests tests/test_favorites_manual_items.py::test_move_preserves_manual_flag
- [x] `get_low_stock` still includes a manual item that is below its own stock thresholds
  verify: tests tests/test_favorites_manual_items.py::test_manual_item_appears_in_low_stock
- [x] SQLite and Postgres schemas both declare the two new columns, with idempotent
      migrations for already-provisioned databases
  verify: present "override_reason" src/kroger_mcp/analytics/pg_database.py
- [x] Full test suite passes and ruff is clean
  verify: cmd python3 -m pytest tests -q -x --timeout=300

## Tasks

- [x] Add `is_manual` + `override_reason` to the SQLite schema and `run_schema_migrations`
- [x] Add the same two columns to PG `SCHEMA_SQL` + `_PG_COLUMN_MIGRATIONS`, and register
      `is_manual` in `database._PG_BOOL_COLS`
- [x] Add `MANUAL_ID_PREFIX`, `new_manual_product_id()`, `is_manual_product_id()` helpers to
      `analytics/favorites.py`
- [x] Teach `add_to_list` / `bulk_add_to_list` the manual path (sentinel generation, prefix
      backstop, `override_reason` persistence)
- [x] Select and return `is_manual` / `override_reason` from all four `get_list_items` query
      variants, plus `check_snacks`
- [x] Add `manual` / `override_reason` params to the `favorites` MCP tool and relax `add_item`
      validation
- [x] Split manual items out of the `order` action's cart path into `manual_purchase`
- [x] Make `AddItemBody.product_id` optional and add `manual` / `override_reason` to the web API
- [x] Handle manual items in `add-to-shopping-list` and `snacks/add-to-list`
- [x] Frontend: manual-item toggle in the Add Item form, MANUAL badge on the row, guarded
      action-menu entries
- [x] Persist `manual_purchase` + `notes` on `user_shopping_lists` (both backends)
- [x] Guard all three caller-facing cart-add entry points against a `manual:` id server-side
- [x] Write `tests/test_favorites_manual_items.py`
- [x] Run the full suite + ruff
