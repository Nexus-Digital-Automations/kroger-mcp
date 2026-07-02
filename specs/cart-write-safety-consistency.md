# Cart-write safety & confirmation consistency

`client.cart.add_to_cart()` — the real, spend-incurring Kroger API call — was
invoked from 4 separate code paths, but the ingredient-safety filter
(`is_filtering_enabled`, `get_all_safe_product_ids`, `get_all_blocked_product_ids`,
`get_disabled_ingredients`, `check_product_safety`) only ran in one of them
(`cart_tools.py`'s `add` action). This violates CLAUDE.md's "Always run
safety(action='check_product') before adding anything to cart" and "Never add
to cart without confirmation" rules. See the approved plan
(`~/.claude/plans/please-look-at-all-abundant-haven.md`, Stream A) for the
original audit.

## Acceptance Criteria
- [x] Shared safety-check helper extracted from cart_tools.py's inline block, reused by every cart-write call site
  verify: present check_cart_items_safety src/kroger_mcp/tools/_cart_safety.py
- [x] cart_tools.py's add action calls the shared helper (no duplicated inline logic)
  verify: present check_cart_items_safety src/kroger_mcp/tools/cart_tools.py
- [x] recipe_tools.py add_to_cart runs the safety check before the real API call
  verify: present check_cart_items_safety src/kroger_mcp/tools/recipe_tools.py
- [x] shopping_list_tools.py add_to_cart runs the safety check before the real API call
  verify: present check_cart_items_safety src/kroger_mcp/tools/shopping_list_tools.py
- [x] favorites_tools.py order action gets a confirm gate and runs the safety check
  verify: present check_cart_items_safety src/kroger_mcp/tools/favorites_tools.py
- [x] cart_tools.py add action's preview_only now defaults to True (bare call previews, matching the other three tools)
  verify: present default=True src/kroger_mcp/tools/cart_tools.py
- [x] Web route meal_plan.py add_plan_to_cart (pushes real Kroger orders) also runs the safety check
  verify: present check_cart_items_safety src/kroger_mcp/web/routes/api/meal_plan.py
- [x] Web route shopping_list.py shopping_list_to_cart (pushes real Kroger orders) also runs the safety check
  verify: present check_cart_items_safety src/kroger_mcp/web/routes/api/shopping_list.py
- [x] meal_plan.html frontend surfaces requires_confirmation and offers a confirm-and-retry dialog instead of silently reporting success
  verify: present requires_confirmation src/kroger_mcp/web/templates/meal_plan.html
- [x] Shopping-list "Send to Kroger Cart" modal (shared across shopping_list.html and recipe pages) surfaces requires_confirmation the same way
  verify: present requires_confirmation src/kroger_mcp/web/templates/_macros/cart_send_modal.html
- [x] ruff is clean on every touched file
  verify: cmd ruff check src/kroger_mcp/tools/_cart_safety.py src/kroger_mcp/tools/cart_tools.py src/kroger_mcp/tools/recipe_tools.py src/kroger_mcp/tools/shopping_list_tools.py src/kroger_mcp/tools/favorites_tools.py src/kroger_mcp/web/routes/api/meal_plan.py src/kroger_mcp/web/routes/api/shopping_list.py

## Decisions
- [x] The pre-existing safety-check response shape (`success`, `requires_confirmation`, `message`, `blocked_items`, `safety_warnings`, `total_flagged`, `items_requested`, `next_step`) is preserved verbatim in the extracted helper so cart_tools.py's own existing callers see no behavior change
- [x] Scope was widened beyond the original 4 MCP-tool call sites to also cover meal_plan.py's and shopping_list.py's web routes, since both push real, unconfirmed orders to the live Kroger API with zero safety check — approved by the user via AskUserQuestion ("Yes, fix both") after discovering the gap mid-implementation
- [x] recipes.py's web route (`add_recipe_to_cart`) was confirmed to be local-cart-bookkeeping only (never calls the real Kroger API), so it was excluded from this fix — no safety gap exists there

## Out of scope
- chat_engine.py's `_handle_add_to_cart` chat-tool path is covered by the multi-tenant user-scoping spec (it needed `user_id` threading, not a new safety check — it already calls into `cart_tools.py`'s already-gated `_add_item_to_local_cart`).
