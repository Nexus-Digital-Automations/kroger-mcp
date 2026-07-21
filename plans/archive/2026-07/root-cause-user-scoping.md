# Root-cause fix: systemic user_id scoping gap

## Context

The comprehensive bug-hunt audit (2026-07-21, memory note `3b195873`) found that this
isn't a handful of isolated bugs — it's one recurring design flaw repeated across the
codebase: dozens of functions in `analytics/` and `tools/` accept an **optional**
`user_id: str | None = None` that silently falls back to a shared "default owner" via
`mcp_user_id()` when omitted. That fallback exists to serve a real, legitimate caller
(MCP tool dispatch has no `request` object), but because it *fails open* instead of
*failing closed*, any web route that forgets to thread the real caller's id through
inherits the shared default instead of erroring — a cross-tenant data leak that's
invisible until someone audits for it by hand.

The codebase already has the correct pattern in two places (`analytics/notifications.py`,
`analytics/ingredient_links.py`): `user_id: str`, no default, caller required to supply
it. The permanent fix is to make that the *only* pattern — remove the silent default
everywhere it's currently optional, and let Python's own "missing required argument"
error do the auditing for us: every currently-broken call site becomes a hard failure
the moment the default is removed, guaranteeing nothing is missed the way a manual
grep-based audit could.

This also closes one real data-model gap (`kroger_order_history.json` is a single
shared file with no user concept at all — not fixable by parameter-threading alone)
and adds a regression test so this exact bug class can't quietly reappear later.

## Decisions

- [x] Full systemic refactor: every function in scope gets a required `user_id: str`
      (no default), not just the ~25 call sites already confirmed broken.
  - verify: present "def test_no_optional_user_id" tests/test_user_scoping_contract.py — the contract test in Task 7 is itself the durable proof this decision holds
- [x] Order history stops using the shared `kroger_order_history.json` file.
      Investigation found the proper user-scoped home already exists and is
      already correctly populated (`orders`/`purchase_events` tables +
      `analytics/purchase_tracker.py`'s `record_order`/`get_order_history` — both
      already called alongside the legacy JSON file, making the JSON path a dead,
      never-migrated duplicate) — no new table needed, just delete the JSON path
      and read/write through the existing DB-backed functions.
  - verify: absent "ORDER_HISTORY_FILE" src/kroger_mcp/tools/cart_tools.py
- [x] A regression-prevention contract test is added so a future PR that reintroduces
      an optional/None-defaulting `user_id` in a scoped module fails CI immediately.
  - verify: tests tests/test_user_scoping_contract.py
- [x] Executed as phased, reviewable commits (DB → analytics layer → MCP callers →
      web routes → contract test), not one giant commit.
  - manual: git log — one commit per phase below, in dependency order

## Design

**Two legitimate boundaries, nothing in between should ever guess.** MCP tool
dispatch (no `request`) resolves identity via `mcp_user_id()`; FastAPI web routes
(always have `request`) resolve identity via `current_user_id(request)` (already
raises 401 if unauthenticated — `src/kroger_mcp/auth/dependencies.py:43`). Every
function *between* those boundaries and the database should take `user_id: str` as
a required, explicit argument — never resolve it itself.

**1. Data layer — order history.** No new table needed: `orders`/`purchase_events`
(`src/kroger_mcp/analytics/database.py:337-360`) already have a `user_id` column
and are already correctly written by `analytics/purchase_tracker.py::record_order`
and read by `::get_order_history` — both already called by every writer of the
legacy JSON file, meaning the JSON path is a dead, never-migrated duplicate left
over from before the DB tables existed. Delete
`_load_order_history`/`_save_order_history`/`_order_history_store`/
`ORDER_HISTORY_FILE`/the `JsonStore` import from `src/kroger_mcp/tools/cart_tools.py`
entirely; `mark_placed`/`view_history` (cart_tools.py) and `mark_order_placed`/
`get_cart_history` (`web/routes/api/cart.py`) call `record_order`/`get_order_history`
directly instead. `get_order_history`'s own optional `user_id` gets fixed in step 2
like every other function in scope. `kroger_order_history.json` stays in
`migrate_to_multi_tenant.py`'s `JSON_FILES_TO_RELOCATE` (already there) — it's
simply orphaned once nothing writes to it anymore, matching the Decision above.

**2. Analytics/tools signature sweep.** Change `user_id: str | None = None` → 
`user_id: str` (required) across every function the audit identified — the pattern
repeats identically in `analytics/favorites.py`, `analytics/pantry.py`,
`analytics/meal_planning.py`, `analytics/safety/*.py`, `analytics/seasonal.py`,
`analytics/consent.py`, `analytics/favorite_depletion.py`, `analytics/predictions.py`,
`analytics/purchase_tracker.py`, `analytics/recommendations.py`,
`analytics/reporting.py`, `analytics/statistics.py`, `analytics/sharing.py`,
`analytics/recipe_integration.py`, `tools/shared.py`, `tools/cart_tools.py`,
`tools/shopping_list_tools.py`. Delete the now-unnecessary optional-resolving
helpers (`analytics/_user_scope.py::resolve_user_id`, `analytics/safety/_common.py`'s
`_resolve_user_id`, and the private `_resolve_*_user_id` wrappers in
`cart_tools.py`/`shopping_list_tools.py`/`tools/shared.py`) once nothing calls them
with `None` — `mcp_user_id()`/`current_user_id()` themselves are kept (they're the
boundary resolvers, not the bug).

Run `pytest` + `mypy` after this step on purpose, before fixing callers — the
failures ARE the audit. Fix every resulting break in steps 3–6, not by re-adding a
default.

**3. MCP tool dispatcher boundary.** In each `register_tools()` module, every
`@mcp.tool()`-decorated function resolves `user_id = mcp_user_id()` once at the top
of its own body and passes it explicitly to every downstream call. Some dispatchers
already do this correctly (`ingredient_management_tools.py:179`,
`meal_planner_tools.py:261`, `favorites_tools.py:240`, `safety_tools.py:140`) —
extend the identical style to `cart_tools.py` and `shopping_list_tools.py`'s
dispatch functions, which currently call helpers with no arguments at all.

**4. Web route boundary.** Every FastAPI handler resolves
`user_id = current_user_id(request)` once at the top and passes it explicitly.
Add `request: Request` to the handful of handlers that don't currently have it
(`get_cart`, `remove_cart_item`, `clear_cart` in `web/routes/api/cart.py`;
`add_recipe_to_list` in `web/routes/api/shopping_list.py`) — FastAPI
dependency-injects it, no route-registration change needed. Thread `user_id`
through the specific calls the audit flagged as "has `request` in scope but forgot
it for this one call": `mark_order_placed`'s cart/order-history/restock calls,
`shopping_list_to_cart`'s list/pantry-preview calls, `add_list_to_shopping_list`'s
pantry-status/list read-write calls.

**5. `products.py` raw SQL.** `deal_watchlist` query gets a `WHERE user_id = ?`
clause (the column already exists). The `favorite_list_items` query has no
`user_id` column of its own — ownership lives one level up — so it becomes a JOIN
through `favorite_lists` (`... JOIN favorite_lists fl ON fli.list_id = fl.id AND
fl.user_id = ?`), matching the join pattern already used in
`analytics/favorites.py::get_list_items`.

**6. `action_menu_context()`.** Add a required `user_id: str` parameter
(`src/kroger_mcp/web/context.py`); update its 7 call sites to pass
`current_user_id(request)`: `web/routes/guides.py` (x2), `web/routes/products.py`,
`web/routes/shopping_list.py`, `web/routes/favorites.py`, `web/routes/recipes.py`
(x2).

**7. Regression contract test.** New `tests/test_user_scoping_contract.py`: walk
the target modules with `inspect`/`ast`, flag any top-level function whose
`user_id` parameter has a `None` default or an `Optional[str]`/`str | None`
annotation, outside an explicit allowlist (`mcp_user_id`, `default_user_id`,
`current_user_id`, `set_web_user_id`, `reset_web_user_id`, and the resolver
functions if any legitimately remain). Assert zero violations — this is what makes
the fix durable instead of a one-time cleanup.

## Acceptance Criteria

- [x] No function in `src/kroger_mcp/analytics/**` or `src/kroger_mcp/tools/**`
      (excluding `auth/dependencies.py`'s boundary resolvers) has an optional
      `user_id` defaulting to `None`
  - verify: tests tests/test_user_scoping_contract.py
- [x] `mark_placed`/`view_history`/`mark_order_placed`/`get_cart_history` read/write
      the existing `orders`/`purchase_events` tables instead of the shared JSON file
  - verify: absent "ORDER_HISTORY_FILE" src/kroger_mcp/tools/cart_tools.py
- [x] `products_page`'s `deal_watchlist` and `favorite_list_items` queries are scoped
      by the logged-in user
  - verify: present "WHERE user_id" src/kroger_mcp/web/routes/products.py
- [x] `action_menu_context` requires `user_id` and every call site passes it
  - verify: absent "def action_menu_context() ->" src/kroger_mcp/web/context.py
- [x] Full existing test suite passes after the refactor
  - verify: tests tests/
- [x] Manual two-account smoke test confirms cart, shopping list, favorites, order
      history, and the products-page watchlist are fully isolated between two
      different logged-in dev users
  - manual: output/user-scoping-smoke-test.md

## Tasks

- [x] Delete `cart_tools.py`'s JSON-backed order-history helpers; route `mark_placed`/`view_history`/`mark_order_placed`/`get_cart_history` through the existing `record_order`/`get_order_history`
- [x] Remove optional `user_id` defaults across all listed `analytics/*` modules; delete now-dead resolver helpers
- [x] Remove optional `user_id` defaults across `tools/shared.py`, `tools/cart_tools.py`, `tools/shopping_list_tools.py`
- [x] Run pytest/mypy, fix every break by threading `user_id` through MCP tool dispatchers (resolve once via `mcp_user_id()` per tool function)
- [x] Fix every break by threading `user_id`/`request` through web route handlers (resolve once via `current_user_id(request)` per handler), adding missing `request` params
- [x] Scope `products.py`'s raw `deal_watchlist`/`favorite_list_items` queries to the logged-in user
- [x] Require `user_id` in `action_menu_context()` and update its 7 call sites
- [x] Add `tests/test_user_scoping_contract.py` regression guard
- [x] Run full test suite; fix any remaining failures
- [x] Manual two-account smoke test against a local dev server; write `output/user-scoping-smoke-test.md`
