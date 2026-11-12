# MCP ↔ web-app feature parity

## Context

The user asked that the MCP server expose everything an agent can usefully do in
the web app, except adding items to the cart. A route-by-route diff of
`src/kroger_mcp/web/routes/api/*.py` (≈110 endpoints) against the 18 MCP tools
(172 actions, introspected from `create_server()`) found these web capabilities
with no MCP equivalent.

## Design

Oracle: `tests/test_mcp_web_parity.py` (param names and return keys there are
the contract). Per tool, mirror the named web handler, and reuse existing tool
params wherever one fits:

- `shopping_list` (`tools/shopping_list_tools.py`). `add_item` takes new params
  `name`, `product_id`, `unit` plus the existing `quantity`/`source`. It mirrors
  `add_shopping_list_item` and fails without `name`. `clear` mirrors
  `clear_shopping_list` and returns `cleared`. `mark_empty`: move
  `_mark_list_items_empty` from `web/routes/api/shopping_list.py` into
  `shopping_list_tools.py`, and have the web module import it from there; the
  name must stay importable from the web module.
- `pantry` (`tools/prediction_tools.py`). `get_history` takes `product_id` and
  a new `days` param (default 30), calls `analytics.pantry.get_usage_history`,
  and returns `events`. `clear` takes a new `confirm` param and refuses
  (`success: False`) unless `confirm=True`. It calls `analytics.pantry.clear_pantry`
  and returns `deleted`. `analytics/pantry.py` must also import `get_db_cursor`,
  because `clear_pantry` uses it but the import is currently missing. The web
  `DELETE /api/pantry` must call `clear_pantry` too.
- `deals` (`tools/deal_tools.py`). `get_watchlist` returns `items`, scoped by
  user. `remove_from_watchlist` takes `product_id`.
- New `notifications` tool in a new `tools/notification_tools.py`, registered
  where the others are (`server.py`). `list` returns the same keys as
  `web/routes/api/notifications.py:get_notifications`, plus `success`.
  `mark_seen` returns `seen`. `dismiss` takes `alert_id` and `acted`; it is a
  not-found (`success: False`) when no row changes.
- `recipes` (`tools/recipe_tools.py`). `cook_preview` takes `recipe_id` and
  `servings`, and calls `meal_planning.preview_recipe_cook`. `mark_cooked`
  takes `recipe_id`, `servings`, and a new `deduct_pantry` (default True), and
  calls `cook_recipe_adhoc`. `undo_cooked` takes a new `cook_event_id`, which is
  required, and calls `undo_recipe_adhoc`. `get_step_times` mirrors
  `web/routes/api/recipes.py:get_recipe_step_times`.
- `meal_plan` (`tools/meal_planner_tools.py`). `list_templates` returns `plans`,
  filtered to templates. `set_template` takes `plan_id` and `is_template`, and
  its UPDATE is scoped by user (`success: False` on not-owned). The web `PATCH
  /template` must reuse it. `create_from_template` takes `source_plan_id`,
  `new_name`, `new_start_date`, and calls `copy_meal_plan`. `confirm_pending`
  calls `confirm_all_pending_meals`. `cook_preview` takes `plan_id`,
  `meal_date`, `meal_slot`, and calls `preview_meal_cook`. `schedule_recipe`:
  move `_ensure_week_plan`, `_displaced_recipe_name`, and the date checks from
  `web/routes/api/meal_plan.py` into `analytics/meal_planning.schedule_recipe(recipe_id,
  meal_date, meal_slot, *, user_id)`, which the web route then calls.
- `favorites` (`tools/favorites_tools.py`). `add_list_to_shopping_list` takes
  `list_id`; `add_snacks_to_list` takes `product_ids`. Move both web handlers'
  bodies (`web/routes/api/favorites.py`) into shared functions that both the
  routes and the tool call.
- `products` (`tools/product_tools.py`). `suggest_for_ingredient` takes
  `search_term` and `limit`, and mirrors `web/routes/api/products.py:suggest_ingredient_products`.
- `info` (`tools/info_tools.py`). `set_include_spices_by_default` takes `value`
  0/1. `set_pantry_deduction_mode` takes a new `mode` param, and a ValueError
  becomes `success: False`. `get_preferences` adds `include_spices_by_default`
  and `meal_plan_pantry_deduction_mode`.
- `web/risk_policy.py` `_OVERRIDES` additions:
  - `recipes`/`meal_plan` `cook_preview` → `read_only`
  - `pantry.clear` → `("hard_blocked", ("confirm", True))`

Analytics functions must be called through their module or imported inside the
function at call time, so that the oracle's monkeypatches take effect.

## Decisions

- [x] Out of scope on purpose: cart adds (the user excluded them), in-app chat (`/api/chat/*`, which is itself an agent), the Kroger credentials form (secrets would land in agent transcripts), and display-only UI state (`favorites-display-mode`, `meal-plan-deduction-notice-seen`).
- [x] `/api/deals/auto` already exists in practice: `deals(action='find')` with no term does a category sweep. The only difference is the web's broader category list, so no new action is added for it.
- [x] New logic is shared, not copied. When a web handler holds logic the MCP needs, it moves into a function that both the route and the tool call, so the two can't drift apart. Every query stays scoped by `user_id`.
- [x] Destructive bulk actions (`pantry.clear`) require `confirm=True`, the same as the web's `?confirmed=true` guard.

## Acceptance Criteria

- [x] `shopping_list`: `add_item` (one-off manual or linked item), `clear`, `mark_empty`.
- [x] `pantry`: `get_history`, `clear` (confirm-gated).
- [x] `deals`: `get_watchlist`, `remove_from_watchlist`.
- [x] New `notifications` tool: `list`, `mark_seen`, `dismiss` (with `acted`).
- [x] `recipes`: `mark_cooked`, `undo_cooked`, `cook_preview`, `get_step_times`.
- [x] `meal_plan`: `list_templates`, `create_from_template`, `set_template`, `confirm_pending`, `cook_preview`, `schedule_recipe`.
- [x] `favorites`: `add_list_to_shopping_list`, `add_snacks_to_list`.
- [x] `products`: `suggest_for_ingredient`.
- [x] `info`: `set_include_spices_by_default`, `set_pantry_deduction_mode` (and both show in `get_preferences`).
- [x] Tests cover every new action, and the full suite, ruff, and mypy are clean.
- [x] The smoke spec (`scripts/smoke_mcp_spec.py`) lists the new actions, so coverage accounting stays complete.
- [x] README / project CLAUDE.md tool docs mention the new actions.
- [ ] Committed and pushed to `main` (done, d3e5ed7), so the pre-push hook deploys to prod. NOT DONE: the hook calls the stale path `automation-platforms/Automation Agent/scripts/deploy-smartshopper-to-mini.sh`, and that script's SRC points at an old `Smart Shopper/` folder that holds only logs. Deploy manually.

## References

- file: tests/test_mcp_web_parity.py
- file: tests/test_risk_policy.py
- file: tests/test_shopping_list_mark_empty.py
- file: src/kroger_mcp/web/risk_policy.py
- file: src/kroger_mcp/server.py
- file: src/kroger_mcp/tools/shopping_list_tools.py
- file: src/kroger_mcp/tools/prediction_tools.py
- file: src/kroger_mcp/tools/deal_tools.py
- file: src/kroger_mcp/tools/recipe_tools.py
- file: src/kroger_mcp/tools/meal_planner_tools.py
- file: src/kroger_mcp/tools/favorites_tools.py
- file: src/kroger_mcp/tools/product_tools.py
- file: src/kroger_mcp/tools/info_tools.py
- file: src/kroger_mcp/analytics/pantry.py
- file: src/kroger_mcp/analytics/meal_planning.py
- file: src/kroger_mcp/analytics/notifications.py
- file: src/kroger_mcp/web/routes/api/shopping_list.py
- file: src/kroger_mcp/web/routes/api/pantry.py
- file: src/kroger_mcp/web/routes/api/deals.py
- file: src/kroger_mcp/web/routes/api/notifications.py
- file: src/kroger_mcp/web/routes/api/recipes.py
- file: src/kroger_mcp/web/routes/api/meal_plan.py
- file: src/kroger_mcp/web/routes/api/favorites.py
- file: src/kroger_mcp/web/routes/api/products.py
- file: src/kroger_mcp/web/routes/api/settings.py
- file: scripts/smoke_mcp_spec.py
- gotcha: every SQL statement must filter by user_id — tests/test_user_scoping_contract.py enforces it.
- gotcha: tools call MCP user via kroger_mcp.auth.dependencies.mcp_user_id(); web routes via current_user_id(request).
- gotcha: run tests with `uv run --frozen python -m pytest`; SQLite and Postgres both supported (tests use skip_on_pg).
- gotcha: do not touch the pre-existing uncommitted plans/*.md edits or data/Chapter 7 Recorded Lecture.txt.
