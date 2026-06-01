# Spec — Smart Ingredient Linking (account-based, recipes global)

## Goal
Make the recipe ingredient-linking popup smart and seamless: it surfaces products the
**logged-in account** has linked/bought before, pre-selects a best guess for one-tap linking,
and suggests standardized ingredient names learned from that account's own history.

## Scope / account model
- Link memory, suggestions, and name standardization are **per-account** (`user_id`).
- **Recipes remain global** — no `user_id` added to recipes; the recipe library is shared.
- Web resolves the account via `current_user_id(request)`; MCP tools via `mcp_user_id()`.

## Acceptance criteria

### Data layer
- [ ] `ingredient_links` table exists after `ensure_initialized()` with columns
      `user_id, norm_name, raw_name, product_id, product_description, times_linked,
      last_linked_at, created_at` and `UNIQUE(user_id, norm_name, product_id)`.
- [ ] Indexes on `(user_id, norm_name)` and `(user_id, norm_name, raw_name)` exist.

### Engine (`analytics/ingredient_links.py`)
- [ ] `normalize_ingredient_name` maps `"Parsley"`, `"fresh parsley"`, `"parsleys"` to the
      same key, mechanically (no curated dictionary).
- [ ] `record_link(user_id, raw, pid, …)` inserts once, then increments `times_linked` and
      refreshes `last_linked_at` on repeat; rows for different `user_id` never collide.
- [ ] `get_canonical_name(user_id, raw)` returns the account's most-used surface form for the
      norm key with a confidence; returns `None` when the typed form is already top or history
      is thin.
- [ ] `suggest_products_for_ingredient(user_id, name)` ranks this account's prior links first,
      then purchase frequency, then pantry/favorites membership, then safety grade; each item
      carries a human `reason`. Cross-account data never leaks.

### Write path (both surfaces feed the same per-account memory)
- [ ] Linking via the web popup (`PUT /api/recipes/{id}/ingredients`) records a link for
      `current_user_id`.
- [ ] Linking via `recipes(action='link_ingredient')` (single + batch) records a link for
      `mcp_user_id()`.
- [ ] Recording is best-effort: a failure never breaks the save/link response.

### API
- [ ] `GET /api/ingredients/suggest?name=…` returns `{canonical_name, canonical_confidence,
      best_guess, suggestions[]}` scoped to the caller's account; 401 when unauthenticated.
- [ ] Cold start (no history) → empty `suggestions`, `best_guess: null` — endpoint still 200.

### Frontend popup (shared partial + JS, used by view & edit)
- [ ] Popover markup extracted to `_macros/ingredient_linker.html`; Alpine component to
      `static/js/ingredient_linker.js`; both templates include them (no inlined duplicate).
- [ ] On open, "Your usuals" section shows above live Kroger results with reason pills.
- [ ] Best guess is pre-highlighted; Enter / one tap links it via existing `linkProduct()`.
- [ ] When canonical differs from typed name, a "Standardize to '<canonical>'?" chip appears;
      accepting renames the ingredient and saves.
- [ ] Cold start with no history behaves exactly as today (live search only) — no regression.

### Repo health
- [ ] `ruff` + `mypy` clean; new engine + normalization unit tests pass (data-integrity path).
- [ ] Playwright MCP E2E validates the usuals section, pre-select, and standardize chip.
- [ ] `git status` clean after commit; no stray root files.
