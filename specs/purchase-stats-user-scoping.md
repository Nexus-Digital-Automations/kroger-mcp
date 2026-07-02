# Purchase-stats, predictions, and reports: multi-tenant scoping

`purchase_events`, `orders`, `product_statistics` had no working per-user
scoping in SQLite despite this being a real, already-multi-tenant production
app (15 real users, see `specs/multi-tenant-user-scoping.md`). This is the
direct continuation of that spec's "Session-3 deferred" items (order history,
MCP tool dispatchers) plus the `product_statistics`/`purchase_tracker.py`/
`statistics.py`/`predictions.py`/`reporting.py` gap identified in this
session's audit (see `~/.claude/plans/please-look-at-all-abundant-haven.md`,
Stream B).

## Acceptance Criteria
- [x] Shared user-scope resolver extracted so statistics/purchase_tracker/reporting all import one implementation
  verify: present resolve_user_id src/kroger_mcp/analytics/_user_scope.py
- [x] purchase_tracker.py record_cart_add/record_order/get_purchase_events/get_order_history are user-scoped
  verify: present resolve_user_id src/kroger_mcp/analytics/purchase_tracker.py
- [x] statistics.py update_product_stats/get_product_statistics/get_all_product_statistics/get_recent_purchases are user-scoped, upsert key is (user_id, product_id)
  verify: present resolve_user_id src/kroger_mcp/analytics/statistics.py
- [x] predictions.py is user-scoped and its in-process memo cache keys by resolved owner (prevents cross-user cache leakage)
  verify: present memo_key src/kroger_mcp/analytics/predictions.py
- [x] recommendations.py's Redis cache key includes the resolved owner (prevents cross-user cache leakage) and its query scopes product_statistics + pantry_items by user
  verify: present resolve_user_id src/kroger_mcp/analytics/recommendations.py
- [x] reporting.py's 5 report functions (spending, prediction-accuracy, patterns, pantry, export_all_data) are all user-scoped
  verify: present resolve_user_id src/kroger_mcp/analytics/reporting.py
- [x] product_statistics SQLite migration rebuilds the PK by structural shape (product_id still sole PK), not by column presence — an earlier one-time script had already bolted a bare user_id column onto this table without rebuilding the PK, which a presence check would have missed
  verify: present bolted src/kroger_mcp/analytics/database.py
- [x] Web/MCP call sites resolve and pass user_id through to the analytics layer (cart.py, chat_engine.py/chat.py, meal_plan.py, recipes.py, shopping_list.py)
  verify: present current_user_id src/kroger_mcp/web/routes/api/cart.py
- [x] chat_engine.py's chat-tool add-to-cart path threads user_id end to end (execute_approved_action → _handle_add_to_cart → _add_item_to_local_cart)
  verify: present user_id=user_id src/kroger_mcp/web/chat_engine.py
- [x] Unit tests prove two distinct user_ids get isolated purchase events, order history, product statistics, predictions, and reports
  verify: tests tests/test_stats_scoping.py
- [x] ruff is clean on every touched analytics/web file
  verify: cmd ruff check src/kroger_mcp/analytics/_user_scope.py src/kroger_mcp/analytics/purchase_tracker.py src/kroger_mcp/analytics/statistics.py src/kroger_mcp/analytics/predictions.py src/kroger_mcp/analytics/recommendations.py src/kroger_mcp/analytics/reporting.py src/kroger_mcp/analytics/database.py src/kroger_mcp/web/chat_engine.py

## Decisions
- [x] Legacy rows that predate multi-tenancy (no reliable attribution signal) stay unattributed — user_id remains NULL, nothing is backfilled to a default user, nothing is deleted. They simply stop surfacing in per-user stats/predictions/reports going forward.
- [x] chat_engine.py's user_id threading is scoped ONLY to the add-to-cart path (`_handle_add_to_cart` / `execute_approved_action` / `chat_approve`), not a full audit of its ~30 handler functions — approved by the user via AskUserQuestion ("Only fix _handle_add_to_cart") to keep this session's diff surgical; the remaining handlers are a follow-up, not a regression (they were never user-scoped before this work either)
- [x] `get_all_favorite_product_ids()` (favorites.py) has no user_id param and queries globally with a 60s Redis cache — a real but low-severity/cosmetic cross-tenant leak (heart-icon-on-search-results annotation only). Explicitly left out of this stream's scope; flagged here as a follow-up rather than fixed silently.
- [x] `pantry_items` lacking a `user_id` column in `initialize_database()`/`run_schema_migrations()` is intentional, not a bug: `mcp_user_id()` itself raises `RuntimeError` demanding the one-time `scripts/migrate_to_multi_tenant.py` run first on any fresh install, and that script is what adds `pantry_items.user_id`. Confirmed via a smoke test, left untouched.
- [x] `products`, `price_history`, `deal_scan_results` stay globally shared (not per-user) — Kroger catalog/pricing data is genuinely global, matching `specs/multi-tenant-user-scoping.md`'s A5.
