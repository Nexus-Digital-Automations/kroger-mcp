# Manual-source items: drop the product_id requirement, model the vendor

## Context

The user buys some groceries at Walmart, not Kroger. Today the system forces
every recipe ingredient to carry a Kroger `product_id` unless the caller sets
`override=True` **and** supplies an `override_reason`. That is friction for the
common case, and even when you take the override path the system has no idea
*where* the item comes from — the vendor survives only as English prose inside
a `notes` string (`"Manual: Not from Kroger"`).

The manual-purchase plumbing itself already exists end to end: a
`manual_purchase` column on `user_shopping_lists` (SQLite + Postgres), items
persisted with `product_id: None`, a `manual_purchase_required` block on order
previews, `manual:<uuid>` synthetic ids for manual favorites, and a shared cart
gate. What is missing is (a) making the unlinked case the *easy* path rather
than an escape hatch, and (b) a real field naming the vendor so the shopping
list can be read as an errand plan.

### The safety invariant this must not break

`check_cart_items_safety` (`src/kroger_mcp/tools/_cart_safety.py`) rejects manual
items unconditionally, ahead of `is_filtering_enabled` and un-bypassable by
`confirm_unsafe`. Its docstring calls this "a structural invariant, not a tunable
preference."

It currently detects manual items **by the `manual:` id prefix only**:

```python
manual_ids = [item["product_id"] for item in items if is_manual_product_id(item["product_id"])]
```

and `is_manual_product_id(None)` returns `False` (verified —
`analytics/favorites.py:36-44`). So the moment unlinked items legitimately carry
`product_id: None`, they sail past the gate, reach `pid = item["product_id"]`
(`_cart_safety.py:86`), and get submitted to Kroger as `{"upc": None}`. Widening
the write path without widening this predicate is the one way this change can do
real damage. Closing it is Acceptance Criterion 1.

## Design

**Manual is derived, never declared.** `product_id` falsy ⇒ manual. The
`override` boolean stops being the source of truth; it is still *accepted* on
input (old recipes on disk carry it) but nothing reads it to make a decision.
This kills the bug class where a caller sets `override=False` on an item with no
`product_id` and produces a row that is neither orderable nor marked manual.

**`source` is free text with known-vendor normalization.** A new
`analytics/manual_sources.py` owns `normalize_source()`: alias-map known vendors
to a canonical spelling (`walmart`/`wal-mart`/`WALMART` → `Walmart`) so they
group cleanly, and pass anything else through with whitespace collapsed but
capitalization intact, so `"Indian grocery"` stays exactly that. Nothing is ever
rejected for being an unknown vendor. Absent/blank → `UNSPECIFIED_SOURCE`
(`"Manual"`), which sorts last.

**That sentinel is for display only.** Writing it to the column would make "the
user never said where" indistinguishable from a store literally called Manual,
and would render the note as `"Buy at Manual"`. So there are two functions, not
one: `normalize_source()` (always a string, for grouping and display) and
`stored_source()` (the canonical name, or `None`, for the column). Every write
path uses the latter. Found the hard way — `add_to_list` persisted the sentinel
and clobbered the `override_reason` note on existing manual favorites, which
`test_favorites_manual_items.py` caught.

**Notes prefer the vendor but never overwrite prose.** `manual_note()` returns
`"Buy at Walmart"` when a vendor is named, else the caller's `override_reason`
(older manual favorites have nothing else), else `None` — an empty note beats a
placeholder, since `source` and `manual_purchase` already say it is an errand.

**DB column is `manual_source`, JSON key is `source`.** `user_shopping_lists`
already has a `recipe_source` column meaning "which recipe did this come from",
so a bare `source` column there would be genuinely ambiguous. The wire/JSON
field stays `source` (what recipe ingredient dicts and the MCP surface use); the
persisted column is `manual_source` in both `user_shopping_lists` and
`favorite_list_items`. Recipe ingredients need no migration at all — recipes are
JSON-stored via `_recipes_store` (`recipe_tools.py:82`), so `source` just rides
along in the ingredient dict.

**Grouping is a shared helper, not per-call-site formatting.**
`group_by_source()` in the same module returns
`[{"source": "Walmart", "item_count": 3, "items": [...]}, ...]` ordered by the
sort rule above. Every surface emits it as `manual_purchase_by_source` *beside*
the existing flat `manual_purchase_required` list, which is preserved verbatim
so no consumer breaks.

### Boy-Scout fix on the path

`shopping_list_tools.py` `add_recipe`'s manual branch (:354-372) sets
`ingredient_name` but not `name`, while `_save_shopping_list` persists
`item.get("name", "")` (:105). Manual items therefore come back from the DB with
an empty name. The linked branch immediately below (:405-424) sets both and
carries a comment explaining exactly this hazard. Fixed here, since this change
makes manual items first-class.

## Decisions

- [x] Manual status is derived from a missing `product_id`, not from the `override` flag; `override` is accepted on input for back-compat but is never the authority for any behavior.
  verify: absent "has_override and not" src/kroger_mcp/tools/recipe_tools.py — the old override-is-authoritative validation branch is gone
- [x] A recipe ingredient with a `name` and no `product_id` validates successfully, with no `override`, `override_reason`, or `source` required.
  verify: tests tests/test_manual_source_items.py -k validate
- [x] Vendor is a persisted free-text `source` field; known vendors (Walmart, Costco, Amazon, Target, Sam's Club, Trader Joe's, Whole Foods, Aldi, H-E-B) normalize to a canonical spelling, unknown values pass through unchanged and are never rejected.
  verify: tests tests/test_manual_source_items.py -k normalize
- [x] Shopping-list and order-preview output groups manual items into per-vendor sections, emitted as `manual_purchase_by_source` alongside the preserved flat `manual_purchase_required` list.
  verify: tests tests/test_manual_source_items.py -k group
- [x] `source` is propagated everywhere manual_purchase already exists: recipes, shopping_list, favorites, and the web API.
  verify: present "manual_source" src/kroger_mcp/analytics/database.py src/kroger_mcp/analytics/pg_database.py src/kroger_mcp/analytics/favorites.py src/kroger_mcp/tools/shopping_list_tools.py
- [x] The UNSPECIFIED_SOURCE sentinel is a display label only — storage writes NULL when no vendor was named, via `stored_source()`. Persisting "Manual" would make "never said where" indistinguishable from a store by that name, and would shadow the `override_reason` note fallback.
  verify: tests tests/test_manual_source_items.py -k "sentinel or normalize_note"
- [x] A manual item's `notes` prefers the vendor but falls back to a legacy `override_reason` rather than overwriting it; with neither, the note is None instead of the nonsense line "Buy at Manual".
  verify: tests tests/test_manual_source_items.py -k normalize_note

## Acceptance Criteria

- [x] An item with a falsy/missing `product_id` is rejected by `check_cart_items_safety` unconditionally — same branch as `manual:` ids, ahead of `is_filtering_enabled`, not bypassable by `confirm_unsafe` or warn_only mode.
  verify: tests tests/test_manual_source_items.py -k cart_gate
- [x] No unlinked item can reach `client.cart.add_to_cart` through any of the six `check_cart_items_safety` call sites or `meal_planner_tools.py`'s inline check.
  verify: tests tests/test_favorites_manual_items.py tests/test_manual_source_items.py
- [x] `check_cart_items_safety` no longer subscripts `item["product_id"]`, so a dict without the key raises no KeyError.
  verify: absent "item\[.product_id.\]" src/kroger_mcp/tools/_cart_safety.py
- [x] The `manual_source` column exists on `user_shopping_lists` and `favorite_list_items` in both backends, added idempotently so an existing DB migrates without data loss.
  verify: tests tests/test_manual_source_items.py -k migration
- [x] A manual item added from a recipe round-trips its name through the DB (the `name`-vs-`ingredient_name` save bug is fixed).
  verify: tests tests/test_manual_source_items.py -k round_trip
- [x] The existing manual-item guard suite still passes unchanged.
  verify: tests tests/test_favorites_manual_items.py
- [x] Lint and type-check are clean.
  verify: cmd .venv/bin/python -m ruff check src tests && .venv/bin/python -m mypy src/kroger_mcp/analytics/manual_sources.py && .venv/bin/python -m black --check tests/test_manual_source_items.py $(git diff --name-only HEAD -- 'src/**/*.py' | tr '\n' ' ') — black is scoped to changed files; ~48 files were already unformatted at HEAD and are out of scope

## Tasks

- [x] Add `src/kroger_mcp/analytics/manual_sources.py` with `KNOWN_SOURCES` alias map, `UNSPECIFIED_SOURCE`, `normalize_source()`, `is_manual_item()`, and `group_by_source()`.
- [x] Harden `_cart_safety.py`: replace the prefix-only check with `is_manual_item()` (falsy pid OR `manual:` prefix), use `.get()` throughout, and render unlinked items by name in the rejection message.
- [x] Relax `_validate_ingredients` in `recipe_tools.py` to require only `name`; update the `ingredients` Field description and the `link_ingredient`/save docstrings to describe `source` instead of `override`/`override_reason`.
- [x] Carry `source` through `recipe_tools.py` save/update/link paths and add `manual_purchase_by_source` to the order preview.
- [x] Migrate `manual_source` onto `user_shopping_lists` and `favorite_list_items` in `analytics/database.py` (CREATE TABLE + `run_schema_migrations`) and `analytics/pg_database.py` (CREATE TABLE + `ADD COLUMN IF NOT EXISTS`).
- [x] Read/write `manual_source` in `_load_shopping_list` / `_save_shopping_list`; derive `manual_purchase` from a falsy `product_id` in `add_recipe`; fix the missing `name` key on the manual branch.
- [x] Emit `manual_purchase_by_source` from the shopping-list view and cart-preview paths in `shopping_list_tools.py`.
- [x] Thread `source` through `analytics/favorites.py` (`add_to_list`, `bulk_add_to_list`, `get_list_items`, `check_snacks`) and `tools/favorites_tools.py` (params + Field descriptions + manual list build).
- [x] Thread `source` through `web/routes/api/shopping_list.py` (`_build_recipe_preview`, `_commit_recipe_items`, `shopping_list_to_cart`) and `web/routes/api/favorites.py`.
- [x] Update `meal_planner_tools.py`'s inline manual check to use the shared `is_manual_item()` predicate.
- [x] Write `tests/test_manual_source_items.py` covering validation, normalization, grouping, the cart gate, migration idempotency, and the name round-trip.
- [x] Rewrite `CLAUDE.md`'s "Recipe Ingredient Requirements" section for the new contract.
- [x] Add `stored_source()` and `manual_note()` and route every persisted write / note through them, so the display sentinel never reaches a column and no user-written reason is overwritten.
- [x] Drop the `override`-gated branch in `scripts/migrate_guides_from_recipes.py` — an unlinked ingredient no longer carries the flag, so the guide line went missing entirely.
- [x] Run ruff + black + mypy and the full test suite.
