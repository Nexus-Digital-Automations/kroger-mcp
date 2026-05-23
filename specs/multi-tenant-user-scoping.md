# Multi-Tenant User Scoping

## Goal

Enforce authentication on every dashboard route, scope all per-user data to the logged-in account, and migrate the current global/anonymous data into a new owner account `jeremyparker` (email `germ576507@gmail.com`).

After this work, two users registering separately must see completely independent recipes, favorites, pantry, meal plans, safety lists, custom ingredients, and shopping lists. The existing data continues to live entirely inside the `jeremyparker` account.

## Done — Acceptance Criteria

A1. **Auth wall**. `AuthMiddleware` is enabled in `src/kroger_mcp/web/app.py`. Anonymous `GET` of any HTML route except `/login`, `/register`, `/logout`, `/callback` redirects to `/login` (302). Anonymous request to any `/api/*` endpoint returns 401.

A2. **Owner account exists**. A user row with email `germ576507@gmail.com`, display_name `jeremyparker`, and bcrypt-hashed password `Jpgermy101` is present after running the migration. The password is never logged or written to disk in plaintext.

A3. **Global data reassigned**. After migration, every existing row in user-scoped tables (see list below) carries `user_id = <jeremyparker.id>`. The migration is idempotent — re-running it is a no-op.

A4. **User-scoped tables**. The following tables gain a non-nullable `user_id` foreign key to `users(id)` and are filtered by current user in every read and constrained in every write:
- `recipes`, `recipe_ingredients` (via recipe_id)
- `favorite_lists`, `favorite_list_items` (via list_id)
- `meal_plans`, `meal_entries` (via plan_id)
- `pantry_items`
- `safe_products`, `blocked_products`
- `ingredient_preferences`, `safety_settings`
- `custom_ingredients`, `ingredient_overrides`
- `deal_watchlist`
- `purchase_events`, `orders`, `product_statistics`

A5. **Catalog tables stay global**: `products`, `price_history`, `whole_foods_catalog`, `seasonal_patterns`, `deal_scan_results`. Reads and writes are unchanged.

A6. **JSON-file state absorbed**. The following root-level JSON files are imported into per-user storage owned by `jeremyparker`, and the on-disk files are moved to `data/legacy/` with a timestamp suffix so they can be restored:
- `kroger_recipes.json` → `recipes` + `recipe_ingredients` (if not already present)
- `kroger_cart.json` → new `user_carts` table or existing cart helper, owned by user
- `kroger_shopping_list.json` → new `user_shopping_lists` table, owned by user
- `kroger_order_history.json` → `orders` + `purchase_events` rows (owned by user)
- `kroger_notion_sync.json` → `user_external_sync` table, owned by user

A7. **Current user available to handlers**. A `CurrentUser` dependency (FastAPI `Depends`) reads `request.state.user` set by the middleware and is injected into every route that touches user-scoped data. Handlers that read `request.state.user` directly are migrated to the dependency.

A8. **No cross-user leakage**. New pytest `tests/test_user_scoping.py` creates user A and user B, writes one entity per scoped table as A, asserts B's read returns 0 rows. Runs as part of `pytest tests/`.

A9. **E2E suite extended**. `tests/e2e/auth.spec.ts` adds a "two-user isolation" test: provisions a second throwaway account, verifies the first account's favorites list is invisible from the second account.

A10. **Existing e2e suite (31 tests) still passes** with the new auth requirement (`authedPage` fixture continues to work; smoke tests use authedPage so they continue to load pages behind auth).

A11. **All Python unit tests pass**: `uv run pytest tests/ -x -q --ignore=tests/e2e --ignore=tests/playwright --ignore=tests/manual` — 234 passed today, must still pass.

A12. **Migration script is one command**: `uv run python -m kroger_mcp.scripts.migrate_to_multi_tenant`. Reads owner creds from `JEREMY_EMAIL` / `JEREMY_PASSWORD` env vars at runtime (defaults documented). Exits 0 on success, non-zero with a clear error otherwise. Logs row counts moved per table.

## Constraints

- **No data loss**. Migration takes a backup of `kroger_analytics.db` to `data/backups/kroger_analytics.db.pre-multi-tenant.{ts}` before any schema change. JSON files are moved (not deleted) to `data/legacy/`.
- **Idempotent**. Re-running migration after success exits 0 with "already migrated" status.
- **Password is never written to a file** in plaintext, never logged, never committed. Only the bcrypt hash lands in SQLite.
- **Backwards-compatible reads at the analytics module boundary**: helper functions (`get_favorite_lists()`, `get_recipes()`, etc.) gain a required `user_id: str` parameter; callers in `src/kroger_mcp/web/routes/` are all updated. MCP server tool entry points get a wrapper that resolves the single-tenant user_id from a settings key `KROGER_MCP_DEFAULT_USER_ID` (set to jeremyparker's id during migration).
- **Single ubiquitous-language term**: every layer uses `user_id` (not `account_id`, `owner_id`, `tenant_id`).
- **Schema migration uses the existing `run_schema_migrations()` mechanism in `database.py`** — no Alembic dependency.

## Approach (chosen)

**Tracer-code path**: end-to-end skeleton first — auth wall + one table (favorites) fully user-scoped + migration framework — then fan out across the remaining 15 tables in parallel.

Phase order:
1. **Skeleton**: Enable middleware. Add `user_id` to `favorite_lists` + `favorite_list_items`. Add `CurrentUser` FastAPI dependency. Update `favorites.py` route + `analytics/favorites.py` queries. Write migration framework (idempotency check, backup, owner creation). Add user-isolation test for favorites. Run e2e — must pass.
2. **Fan-out**: For each remaining user-scoped table, add `user_id` column, update analytics helper signatures, update route handlers, extend migration to backfill from `jeremyparker.id`.
3. **JSON-file absorption**: Read each root JSON file, write rows into per-user tables, move the file to `data/legacy/`.
4. **Verify**: full pytest + full e2e + the new two-user isolation tests + manual smoke against `http://127.0.0.1:8000`.

## Out of scope

- OAuth / SSO. Local password auth only.
- Password reset / email verification flows (the existing `/register` has no reset; not adding it now).
- Per-user MCP token isolation (single Kroger OAuth token shared by all users — the existing model). Will note this in the README as a follow-up.
- Per-user file uploads or assets.

## Risks

- **Schema-change footprint**: 16 tables, each requires `ALTER TABLE ADD COLUMN user_id` plus an index. SQLite `ALTER TABLE` supports adding nullable columns; we backfill from `jeremyparker.id` then add a CHECK or trigger. Postgres path needs equivalent migration.
- **MCP-server entry points are not authenticated** — they're tools called by Claude Desktop, not HTTP. We default them to `KROGER_MCP_DEFAULT_USER_ID` (jeremyparker) and document the limitation.
- **Background scanner** (`scripts/background_scanner.py`) writes to `deal_scan_results` (global) — unaffected. But anything writing to user-scoped tables from background jobs needs the same default-user resolution.

## Test plan

- New `tests/test_user_scoping.py`: parametric across every scoped table, asserts cross-user isolation.
- Extend `tests/e2e/auth.spec.ts` with two-user isolation.
- Extend `tests/e2e/00-smoke.spec.ts` to assert unauthed routes redirect to `/login`.
- All 234 existing pytest cases continue to pass.
- All 31 existing e2e specs continue to pass.

## Session-1 status (delivered)

- Migration script + idempotent re-run + 20-table backfill + JSON-file relocation + `favorite_lists` UNIQUE-constraint drop.
- `AuthMiddleware` enabled — anonymous HTML → 302 /login, anonymous /api/* → 401.
- `CurrentUser` + `default_user_id()` dependencies in `src/kroger_mcp/auth/dependencies.py`.
- Favorites end-to-end user-scoped: `create_list`, `get_lists`, `get_list`, `get_list_items`, `delete_list` accept `user_id` and filter/insert with it. HTML routes (`/favorites`, `/favorites/{id}`) and API routes (`/api/favorites/lists*`) pass `current_user_id(request)` through.
- `jeremyparker` owner account created with the supplied credentials, owns all backfilled rows.
- pytest: 241 passed, 2 skipped (was 234) — 7 new isolation tests verify user A cannot see/delete user B's favorites.
- e2e: 29 passed, 2 skipped (recipes tests skip on accounts with no recipes, which is the correct fresh-user state).

## Session-2 status (delivered)

Commits: `5d26a83` (migration v2 + mcp_user_id), `9f282ef` (pantry/meals/safety/ingredients), `15185f4` (dashboard + isolation tests), `5c58d47` (spec), `5d387c9` (deals watchlist), `50f561b` (shared.py preferences → user_settings), `bfab649` (settings API), `aa9f6fd` (spec session-3 notes), `1af014f` (cart_tools DB migration), `1666c9a` (shopping_list_tools DB migration), `3859cd3` (shopping_list API endpoints).

**Migration v2** — extended `scripts/migrate_to_multi_tenant.py`:
- New tables: `user_carts`, `user_shopping_lists`, `user_notion_sync`, `user_settings`.
- Generic `_recreate_table_without_unique` helper; 8 tables recreated with composite `UNIQUE(user_id, key)` / `PRIMARY KEY(user_id, key)`: `custom_ingredients`, `ingredient_overrides`, `ingredient_preferences`, `safe_products`, `blocked_products`, `pantry_items`, `deal_watchlist`, `safety_settings`.
- `PRAGMA foreign_keys=OFF` during recreate prevents cascade-delete (lesson learned from session 1).
- `_absorb_legacy_json` imports data/legacy/kroger_cart.json + kroger_notion_sync.json into the new tables. Idempotent.

**`mcp_user_id()`** added to `src/kroger_mcp/auth/dependencies.py`:
- Reads `KROGER_MCP_USER_ID` per Claude Desktop config first, falls back to `KROGER_MCP_DEFAULT_USER_ID`.
- Raises `RuntimeError` if neither set — surfaces misconfig loudly.

**Modules fully scoped (analytics + routes + MCP tools)**:
- favorites (session 1)
- pantry: 11 analytics functions + routes + meal_planner_tools
- meal_planning: 19 analytics functions + routes
- safety: 14+ analytics functions + routes + safety_tools + ingredient_management_tools (auto-seeds default safety_settings for new users)
- ingredients: routes/api/ingredients + routes/ingredients + ingredient_management_tools
- dashboard: 4 aggregation helpers now take user_id; dashboard_page resolves from session

**pytest: 247 passed, 2 skipped** (was 234 baseline). 13 isolation tests covering:
- favorites (4 cases)
- pantry (1)
- meal plans (1)
- safety (2)
- mcp_user_id resolver contract (3)
- default_user_id resolver contract (3)

## Session-3 deferred (still needs work)

Migrated to DB this session: cart, shopping_list, user_settings (location/servings/spices/credentials/product_sort). The items below are what still rides on `default_user_id()` fallback or has file-backed storage.

| Surface | Status today | Concrete next step |
|---|---|---|
| recipes (storage) | Still backed by `kroger_recipes.json` at project root — shared file. Restored after migration relocated it. | Add a recipes-absorption step to `migrate_to_multi_tenant.py` that imports each JSON entry as a `recipes` row owned by the migration owner. Then change `tools/recipe_tools.py::_load_recipes / _save_recipes` to query the DB. |
| recipes (routes) | `routes/recipes.py`, `routes/api/recipes.py` don't take `user_id`. Mutations write to the shared JSON. | After the storage rewrite, every route handler takes `request: Request` and threads `current_user_id(request)` to the recipe layer. |
| shopping list (composite endpoints) | 5 of 8 endpoints in `routes/api/shopping_list.py` now scoped. Still pending: `POST /api/shopping-list/add-recipe` and `POST /api/shopping-list/add-to-cart` — they call `_commit_recipe_items` and cart/recipe tools that need user_id threading. | Add `request: Request` to those two handlers, then thread `user_id` through `_commit_recipe_items` to `_save_shopping_list`. The cart side already supports user_id. |
| order history | `tools/cart_tools.py` still uses `JsonStore(Path("kroger_order_history.json"))` for the cart-history view. File relocated by migration → empty list. Per-user orders live in the `orders` + `purchase_events` DB tables which already have `user_id`. | Rewrite `_load_order_history` / `_save_order_history` to read/write `orders` + `purchase_events` for the resolved user. |
| MCP tools (7 modules) | `deal_tools`, `favorites_tools`, `notion_tools`, `prediction_tools`, `reporting_tools`, `info_tools`, `recipe_tools` still rely on `mcp_user_id()` fallback inside the analytics layer. Works for jeremyparker (his ID is the default). To support a second Claude Desktop profile, each `_*_impl` dispatcher should call `user_id = mcp_user_id()` and pass through explicitly. | Add the `mcp_user_id()` call at the top of each action dispatcher; pass `user_id=user_id` to every analytics call within. |
| E2E two-user isolation spec | Not written. | New `tests/e2e/two-user-isolation.spec.ts`: register a second account via `/register`, log in as B, GET `/api/favorites/lists` + `/api/pantry` + `/api/shopping-list` etc., assert each returns an empty payload while a parallel jeremyparker session sees populated data. |

Pattern to apply (mirrors favorites):
1. Each analytics function gains `user_id: str | None = None`; uses `_resolve_user_id` at the top.
2. Every SQL `WHERE` filters by `user_id`; every `INSERT` includes it.
3. Each route handler accepts `request: Request`, calls `current_user_id(request)`, passes through.
4. Each MCP action dispatcher calls `user_id = mcp_user_id()` and passes through.
5. Add cross-user pytest cases analogous to `TestPantryScoping`/`TestSafetyScoping`.

## Rollback

```bash
# 1. Restore DB from backup
cp data/backups/kroger_analytics.db.pre-multi-tenant.{ts} data/kroger_analytics.db

# 2. Restore JSON files
mv data/legacy/kroger_*.json.{ts} ./

# 3. Revert the commit range
git revert <commit-range>
```
