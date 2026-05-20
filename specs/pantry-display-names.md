# Spec — Pantry rows render product names, not raw IDs

## Problem

`/pantry` renders bare numeric `product_id`s (e.g. `0001111015405`) instead of
human-readable descriptions. The Jinja template falls back to ID when
`pantry_items.description` is NULL, and the write paths that insert pantry rows
are not persisting the description that's already available to them.

## Acceptance criteria

1. **Write path — list → cart.** Adding any item to the Kroger cart via
   `POST /api/shopping-list/add-to-cart` (the preview-confirm flow) results in
   a `pantry_items` row whose `description` equals the list item's `name`. If
   the source item has no name, falls back to existing behaviour (NULL → ID).
2. **Write path — MCP `cart(action='add')`.** Adding any item via the
   FastMCP `cart` tool results in a `pantry_items` row whose `description`
   equals the `description` carried on the formatted cart item. The
   safety-check code at `cart_tools.py:337` already proves this field is
   populated on that path.
3. **Backfill — local first.** `scripts/backfill_pantry_names.py` updates every
   `pantry_items` row where `description IS NULL OR description = ''` by
   copying from `products.description` when present.
4. **Backfill — Kroger fallback.** Rows still nameless after step 3 are
   resolved via `client.product.get_product(product_id=..., location_id=...)`
   using `get_client_credentials_client`. The recovered description is written
   to **both** `products.description` and `pantry_items.description` so future
   cold-start writers can use the local fallback in `add_to_pantry`.
5. **Idempotency.** Re-running the backfill performs zero updates and exits
   cleanly.
6. **No regressions.** `tests/test_auto_pantry_direct.py`,
   `tests/test_pantry_expiration.py`, and the Playwright journey suite
   (`tests/playwright/test_full_journeys.js`, 41 assertions) all pass.
7. **Whole-repo clean.** `ruff check src/ scripts/` and the project's
   typecheck both pass with zero errors.

## Out of scope

- DB schema migration (`description TEXT` already exists).
- UI/template changes (template already prefers `description` over
  `product_id`).
- Touching `add_to_pantry`, `restock_item`, or `update_pantry_level` —
  they already persist `description` correctly when given one.

## Verification

- Manual: add one item via the recipe → list → cart preview; reload `/pantry`;
  observe the human name. Run `python scripts/backfill_pantry_names.py`;
  reload `/pantry`; observe prior nameless rows are renamed. Re-run the
  script; observe `0 updated`.
- Automated: `ruff check`, project typecheck, both pytest pantry modules,
  Playwright journey suite — all green.
