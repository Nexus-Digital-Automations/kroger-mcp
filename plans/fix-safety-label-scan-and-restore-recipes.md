# Fix safety false-clean scan + restore removed recipes

## Context

Two findings from the 2026-08-02 session, both confirmed against live data.

**1. `safety(action='check_product'/'check_products')` scores products on their
name, not their label.** `check_product_safety()` in `analytics/ingredients.py`
opens with `text = description.lower()` and scans only that string. A product's
*name* essentially never lists its additives, so the check finds zero bad
ingredients on anything ultra-processed, while the wholesome words in the name
earn positive-attribute bonuses.

Measured: Kroger condensed cream of chicken soup `0001111016044` scored
**95/A with zero flagged ingredients**, matching `"chicken"` → "Lean Protein"
and `"cream"` → "Natural Dairy". `recipes(action='analyze')` on the *same
product_id* flagged **seven** additives (Modified Food Starch, Soy Protein
Isolate ×2, Soy Lecithin ×2, Autolyzed Yeast Extract, Maltodextrin, Natural
Flavors).

The label data was there the whole time. `analytics/recipe_scoring.py` reads
`products.ingredients_text` and prefers it over the description
(recipe_scoring.py:177-185); the safety path never does that lookup. CLAUDE.md
mandates running the safety tool before every cart add, so this is the one code
path whose entire job is catching these products, and it waves them through.

**2. Seven recipes disappeared from the store between 2026-06-19 and today.**
Diffing the live store against `kroger_recipes.prod.20260619-214156.bak` **by
id** (names mislead — renames look like deletions) accounts for all seven:
three were migrated into the guides store, four have no counterpart anywhere.

## Design

**Safety fix** — make the safety path resolve the same authoritative text the
recipe path already does. One shared rule, used by both, so they cannot drift
apart again:

- `resolve_scan_text(description, brand, ingredients_text)` in
  `analytics/ingredients.py`, next to `check_product_safety` (both callers
  already import from there, so no new import edges and no cycle).
- `_load_ingredients_text(product_ids)` in `analytics/safety/checks.py` —
  one batched `SELECT` for the whole request, mirroring the batch load
  recipe scoring already does. Uses `?` placeholders, which
  `database._translate_sql` rewrites to `%s` for Postgres (verified — this
  is the same dialect trap that bit `save_scan_results()` last session).
- Both `get_product_safety_status` and `check_products_safety_batch` scan the
  resolved text. A DB failure falls back to today's description-only behavior
  rather than raising — a degraded check beats a broken tool.

The Redis memo key in `safety/_cache.py` already folds the scanned text into
its hash, so feeding it label text auto-misses stale name-scanned entries. No
cache-busting needed.

**Restore** — additive merge into the live store on the mini, keyed by original
recipe id so existing references stay valid, with a timestamped backup taken
first. Only the four recipes absent from *both* stores are restored; the three
that became guides are deliberately skipped, since re-adding them would
duplicate live guides.

## Decisions

- [x] Scan the real label (`products.ingredients_text`) when it exists, falling
      back to description + brand — the exact rule recipe scoring already uses
  verify: present "def resolve_scan_text" src/kroger_mcp/analytics/ingredients.py
- [x] Both the safety path and the recipe path call one shared helper, so the
      two can no longer disagree about the same product
  verify: present "resolve_scan_text" src/kroger_mcp/analytics/recipe_scoring.py
- [x] Label lookup is one batched query per request, not one per product
  verify: present "def _load_ingredients_text" src/kroger_mcp/analytics/safety/checks.py
- [x] A label-lookup failure degrades to description-only scanning instead of
      raising — the safety tool must never hard-fail a cart add
  verify: tests tests/test_safety_label_scan.py -k degrades
- [x] Restore only the 4 recipes missing from both stores; skip the 3 that
      became guides, which would otherwise duplicate live guide entries
  manual: plans/fix-safety-label-scan-and-restore-recipes.md — Results: restore log lists 4 ids
- [x] Preserve original recipe ids on restore so any existing reference resolves
  manual: plans/fix-safety-label-scan-and-restore-recipes.md — Results: ids match the backup

## Acceptance Criteria

- [x] A product whose name is wholesome but whose label is not (the cream of
      chicken case) no longer scores as clean
  verify: tests tests/test_safety_label_scan.py -k name_clean_label_dirty
- [x] `check_product` and `recipes analyze` agree on the same product_id
  verify: tests tests/test_safety_label_scan.py -k agrees_with_recipe_path
- [x] Products with no cached label still scan exactly as they do today
  verify: tests tests/test_safety_label_scan.py -k no_label_unchanged
- [x] The live store holds 68 recipes and all 4 restored ids resolve
  manual: plans/fix-safety-label-scan-and-restore-recipes.md — Results: post-restore count
- [x] Repo stays lint/type clean
  verify: cmd uv run ruff check src/kroger_mcp/analytics/ && uv run mypy src/kroger_mcp/analytics/safety/checks.py src/kroger_mcp/analytics/recipe_scoring.py
- [x] Full suite still passes
  verify: tests tests/ --ignore=tests/test_etl_sqlite_to_pg.py — split from the ETL file below: full `tests/` now runs ~134s against a 120s DSL cap (678 passed, 2 skipped, confirmed 2026-08-06), and the ETL/PG round-trip tests alone account for ~55s of that
  verify: tests tests/test_etl_sqlite_to_pg.py

## Tasks

- [x] Add `resolve_scan_text()` to `analytics/ingredients.py`
- [x] Add `_load_ingredients_text()` and wire both entry points in `safety/checks.py`
- [x] Switch `recipe_scoring.py` to the shared helper
- [x] Write `tests/test_safety_label_scan.py` regression tests
- [x] Back up the live recipe store, restore the 4 recipes, verify the count
- [x] Run ruff + mypy + full pytest

## Results

### Safety fix

`resolve_scan_text()` now owns the "which text is authoritative" rule, and both
callers use it — `safety/checks.py` (both `get_product_safety_status` and
`check_products_safety_batch`) and `recipe_scoring.py`. The two paths can no
longer reach different verdicts about the same product_id, which was the actual
defect: the data to catch the soup was in `products.ingredients_text` all along,
and only one of the two readers looked at it.

Label loading is one batched query per request. `?` placeholders are correct
here — `database._translate_sql` rewrites them to `%s` for Postgres (checked
before writing the query, since SQLite-only SQL in a save path was last
session's bug).

The Redis memo in `safety/_cache.py` needed no change: its key already hashes
the scanned text, so feeding it label text misses the stale name-scanned
entries instead of returning them.

### Verification

- `tests/test_safety_label_scan.py` — **13 passed**. The regression is pinned by
  a pair, not a single assertion: `test_name_only_scan_would_have_missed_it`
  asserts the product *name* yields zero matches, and
  `test_name_clean_label_dirty_is_flagged` asserts the wired path does flag it.
  If the lookup is ever unwired, the second fails.
- Full suite: **678 passed, 2 skipped** (665 before + 13 new; the 2 skips are
  pre-existing in `tests/test_bulk_operations.py`).
- `ruff check src/kroger_mcp/analytics/ tests/test_safety_label_scan.py` — clean.
- `mypy` on both changed modules — clean.

### Live prod verification (commit `0bc4e59`, deployed)

Ran the deployed code on the mini against the real product from the finding:

| | Before | After |
|---|---|---|
| score / grade | 95 / A | **31 / F** |
| flagged ingredients | 0 | **8** |
| status | clean | **AVOID** |

The label was in the DB all along — `CHICKEN STOCK, MODIFIED CORN STARCH,
COOKED CHICKEN MEAT, WHEAT FLOUR, … SOY PROTEIN CONCENTRATE, … YEAST EXTRACT…`
Matches: Modified Food Starch, Soy Protein Isolate ×2, Soy Lecithin ×2,
Autolyzed Yeast Extract, Maltodextrin, Natural Flavors.

Positive attributes now match against the label rather than the name
(`onion`, `chicken`, `milk`, `flour`) — those really are in the product, so
the bonuses are earned rather than inferred from marketing copy.

Note: the deploy hook reloads the web app immediately but the **Kroger MCP
server only reloads on the next session**, so MCP tool calls in this session
still ran the pre-fix code. Verification above bypassed the MCP server and
called the deployed module directly.

### Restore

Ran `output/recipe-restore/restore_removed_recipes.py` on the mini. Store went
**64 → 68**. Backup taken first at
`kroger_recipes.json.prerestore.20260802-130436.bak`; written via temp+rename
because `JsonStore.save()` writes in place and an interrupted write would
truncate the store.

Restored with original ids preserved:

| id | name |
|---|---|
| `56c18405` | Spicy Sausage & Chicken Gizzard Dirty Rice with MSG Boost |
| `f4da8b47` | Improved Savory Ground Beef Stroganoff |
| `b786c4dd` | Cranberry Chicken Salad on Apple Slices (7 ingredients) |
| `f0f16923` | Cranberry Chicken Salad on Apple Slices (10 ingredients) |

Verified beyond the file: `recipes(action='get', recipe_id='f4da8b47')` through
the live MCP tool returns the recipe with all 13 ingredients.

The three that became guides on 2026-06-19 (`9fa2ad84`, `04d2020b`, `fbb8e5c9`)
were deliberately **not** restored — they exist in the guides store under new
ids (`021992ac`, `c7f8bdc9`, `f427626c`), so re-adding them would duplicate live
guides. Easy to add back if the user wants them under Recipes too.

### Note on the duplicate pair

`b786c4dd` and `f0f16923` are both "Cranberry Chicken Salad on Apple Slices"
(7 vs 10 ingredients, created 7 seconds apart). Both were restored rather than
picking one, since choosing is the user's call. A newer
`9308d52b` "Elevated Apple-Cranberry Chicken Salad" also exists.

<!-- last-verified: 2026-08-02 -->
