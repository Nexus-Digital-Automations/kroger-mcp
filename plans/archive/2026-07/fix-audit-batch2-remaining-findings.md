# Fix batch 2: remaining findings from the July 21 bug-hunt follow-up

## Context

After fixing the first 9 named findings from the 2026-07-21 audit, a fresh
targeted bug hunt (5 parallel investigations over cart, favorites/shopping-list,
pantry/meal-plan/recipes, safety/ingredients, templates/frontend) surfaced 23
more candidate bugs. Two of the most severe (safety block_mode enforcement gap,
cross-tenant custom-ingredients leak) were independently re-verified directly
against the code before being reported. This plan fixes all 23.

## Decisions

### Safety/ingredient module (health-critical — fix first, most carefully)
- [x] `block_mode='hard'` is never enforced (`_cart_safety.py` never calls
      `get_block_mode()`) — hard mode must actually block cart adds instead of
      only warning, when the batch has blocked/flagged items and the user
      hasn't set confirm_unsafe=True.
      verify: present "get_block_mode" src/kroger_mcp/tools/_cart_safety.py
- [x] `get_active_ingredients()` and its overrides query have zero `user_id`
      scoping on `custom_ingredients`/`ingredient_overrides` — must accept and
      apply `user_id` so one tenant's custom ingredients/overrides don't leak
      into every other tenant's safety scans.
- [x] Safety cache key (`_cache.py`) must include a hash of the actual
      description/brand text being scanned, not just product_id, so a changed
      description can't return a stale verdict.
- [x] Scoring math must not let positive-attribute bonus fully offset a
      CRITICAL severity match into an "acceptable"/"good" headline status.
- [x] `web/routes/safety.py`'s 4 bare `except Exception: pass` blocks must log
      the error instead of swallowing silently.

### Cart/checkout pipeline
- [x] `_save_cart_data`/`_load_cart_data` race: two near-simultaneous writes
      for the same user can lose an item. Serialize writes per user (a simple
      in-process lock keyed by user_id is sufficient given this is a
      single-process app; document the constraint).
- [x] Price-observation dead code: `_add_item_to_local_cart` looks for a
      nested `product_details["pricing"]` that no caller ever sets — wire it
      to the flat `price` field callers actually pass, or drop the dead branch
      if reconstructing real pricing data isn't feasible from present callers.
- [x] `web/routes/products.py`'s add-to-cart route silently swallows local-
      tracking failures — match the already-fixed sibling pattern: log the
      error and surface a `local_tracking_warning` instead of a bare `pass`.
- [x] `calculate_cart_savings` always computes ~0% because no writer ever sets
      `regular_price` on a cart item — wire `regular_price` through from the
      product data callers already have (`price` fields from Kroger include a
      regular vs promo price) so savings reflect reality.
- [x] Enforce the documented 1-99 quantity bound on cart-add (MCP tool field
      and the two Pydantic request bodies) instead of accepting 0/negative
      straight through to the real Kroger API call.

### Favorites/shopping-list pipeline
- [x] `list_id="default"` sentinel doesn't match any real per-user list id
      (`default-<uuid>`) — resolve the caller's actual default list id
      instead of the literal string "default" wherever it's used as a
      fallback, and fix `get_list_items`' unscoped `IS_DEFAULT` JOIN to
      filter by user_id.
- [x] MCP `add_recipe` shopping-list path drops ingredient name/recipe
      attribution on reload (writes `ingredient_name`/`sources`, but the
      loader reads `name`/`recipe_source`) — align with the already-fixed
      web-route path (`_commit_recipe_items`) that writes both.
- [x] `suggest_for_list` has zero user scoping despite querying a
      user-scoped table — thread `user_id` through from `favorites_tools.py`.
- [x] `web/routes/api/shopping_list.py`'s add-to-cart route silently swallows
      local-tracking failures — same fix pattern as the cart-route fix above.

### Pantry/meal-plan/recipes pipeline
- [x] Shopping-list consolidation drops quantity (silently) when two recipes
      use different units for the same ingredient — surface the mismatch
      instead of silently dropping the second recipe's quantity (list both
      as separate line items when units don't match, rather than merging).
- [x] `consume_from_pantry`'s percent-only path writes a fabricated `quantity`
      (`1`) and stores a percent value in the unit-count `quantity_delta`
      column — record percent-only consumption distinctly instead of
      fabricating unit data.
- [x] Recipe cost estimate + its cache key ignore ingredient quantity —
      multiply by quantity and include it in the cache-key hash.
- [x] Quick-add-ingredient route doesn't call `_teach_link_memory` like the
      bulk-replace path does — call it after a successful quick-add too.

### Templates/frontend
- [x] Search/deals results have no request-sequencing guard — apply the same
      stale-response guard pattern already used in `linker_popover.js`.
- [x] `api-client.js`'s error toast never fires when the error response isn't
      valid JSON — fall back to a generic message instead of letting the
      `res.json()` rejection swallow the whole error path.
- [x] `sale_price === 0` incorrectly falls back to `regular_price` — use an
      explicit `!= null` check instead of `||` for price fields. Found in 5
      files, not the originally-estimated 4 (`deal_card.html`'s "Save $" line
      has the same bug and was fixed too): `deals.html`, `products.html`,
      `_macros/ingredient_linker_popover.html`, `_macros/product_picker.html`,
      `_macros/deal_card.html`.
- [x] Favorites-detail cart/list buttons need a double-submit guard matching
      the pattern used on `products.html`/`deals.html`.
- [x] Notification unseen-badge race: an in-flight poll response landing
      after the user dismisses the badge can resurrect it — guard with a
      request-sequence token or skip applying a poll result while the panel
      is open and freshly dismissed.

## Acceptance Criteria

- [x] All 23 items above have a corresponding code change (or a documented,
      justified decision not to change behavior, if investigation shows the
      original finding doesn't hold up under closer inspection).
- [x] ruff + mypy clean on all touched files.
      verify: cmd cd "$(git rev-parse --show-toplevel)" && ruff check . && mypy src
- [x] Full pytest suite passes.
      verify: tests tests
- [x] No new cross-tenant data leaks introduced by any fix (spot-checked).

## Tasks

- [x] Fix safety/ingredient module (5 items)
- [x] Fix cart/checkout pipeline (5 items)
- [x] Fix favorites/shopping-list pipeline (4 items)
- [x] Fix pantry/meal-plan/recipes pipeline (4 items)
- [x] Fix templates/frontend (5 items)
- [x] Run ruff/mypy/pytest across the whole repo, fix any fallout
      (found and fixed pre-existing fallout unrelated to today's 23 items:
      2 mypy errors from an earlier user_id refactor in
      scripts/backfill_pantry_names.py and web/routes/recipes.py, plus a
      missing `run_schema_migrations()` call in
      tests/test_ingredient_integration.py's clean_db fixture that left
      custom_ingredients without its user_id column in the isolated test DB)
- [x] Commit and push
