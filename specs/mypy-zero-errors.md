# Spec: Drive the repo to zero mypy errors

## Goal
`uv run mypy` over `src/kroger_mcp/` reports **0 errors** under the project's
existing `[tool.mypy]` config (python 3.10, `ignore_missing_imports=true`,
`warn_unused_ignores`, `warn_redundant_casts`; `tests/` excluded). No switch to
strict mode — fix the real errors the current config surfaces.

## Approach (locked)
- Real code fixes for our own code.
- Third-party untyped imports: install stubs (`types-requests`) — preferred over
  per-module ignores.
- `# type: ignore[code]` only where typing is genuinely intractable, each with a
  one-line WHY comment. No blanket suppression, no per-module `ignore_errors`.
- Behavior-preserving except for the three real bugs below, which get correct fixes.

## Real bugs found via the type-check (fix properly, not silence)
1. **`analytics/meal_planning.py:1888` → `pantry.check_pantry_quantity`** — function
   does not exist; imported unconditionally → runtime crash on the meal-plan
   pantry-check path. Implement the check (using `get_pantry_item` /
   `consume_from_pantry` semantics) to return `{in_pantry, has_enough, ...}` as the
   call site expects, or route to the correct existing helper.
2. **`web/chat_engine.py:483` → `recipe_integration.get_cookable_recipes`** — wrong
   name; rename to `get_recipes_for_pantry` (the name `tools/reporting_tools.py`
   already uses). Verify return shape matches usage.
3. **`analytics/meal_planning.py:1096,1241,1873` → `recipe_tools._collect_ingredients_recursive`**
   — does not exist; try/except always falls back. Remove the dead import and use
   the fallback path directly (or implement if the recursive collect is intended).

## Error buckets (≈109 errors, from baseline mypy run)
- **ctx default (~22):** `ctx: Context = None` → `ctx: Context | None = None` across
  `tools/*.py` + `config/prompts.py`. Mechanical.
- **untyped imports (2):** `requests` in `tools/product_tools.py`, `web/chat_engine.py`.
- **var-annotated (~10):** add annotations (predictions, recommendations,
  recipe_integration, chat_engine, server, web/routes/recipes, api/shopping_list).
- **object-typed JSON in analytics (~40):** statistics, seasonal, migration,
  reporting, meal_planning, ingredients — annotate/cast loaded data instead of `object`.
- **assignment/arg mismatches (rest):** deals, safety, cart_tools, api/cart,
  recipes route, meal_plan route, purchase_tracker, recommendations, recipe_integration.

## Acceptance criteria
- [ ] `uv run mypy src/kroger_mcp` → `Success: no issues found` (0 errors).
- [ ] The 3 real bugs fixed with correct behavior (not `# type: ignore`).
- [ ] `types-requests` added to dev dependencies; no new `ignore_missing_imports`
      per-module hacks beyond what already exists.
- [ ] `# type: ignore` count is minimal and each carries a WHY comment.
- [ ] `ruff` clean on all changed files; `warn_unused_ignores` does not fire.
- [ ] Branch `chore/mypy-zero-errors`; committed + pushed. No behavior regressions
      in the app's import-time/startup paths (spot-check: `uv run python -c "import kroger_mcp.web.app"`).

## Verification
1. `uv run mypy src/kroger_mcp` → 0 errors.
2. `uv run python -c "import kroger_mcp.web.app; import kroger_mcp.analytics.meal_planning"` — imports clean.
3. Exercise the two crash-path bugs if feasible (meal-plan pantry preview; chat cookable-recipes tool) or at least confirm the functions resolve.
4. `ruff check` on changed files.
