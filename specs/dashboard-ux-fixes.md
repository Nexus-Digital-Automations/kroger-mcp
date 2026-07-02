# Web dashboard UX fixes

Small, contained UX gaps found during this session's full-codebase feature
audit (see `~/.claude/plans/please-look-at-all-abundant-haven.md`, Stream C):
a destructive action with no confirmation, a safety-critical settings page
that silently swallows errors, and a scattering of similar small gaps across
other pages.

## Acceptance Criteria
- [x] meal_plan.html deletePlan() confirms before deleting an entire meal plan (mirrors recipes.html's deleteRecipe())
  verify: present ssConfirm src/kroger_mcp/web/templates/meal_plan.html
- [x] safety.html no longer silently swallows fetch errors on approve/block/unapprove/unblock actions; failures surface via the page's existing message banner
  verify: present showMsg src/kroger_mcp/web/templates/safety.html
- [x] favorites_detail.html removeItem() confirms before removing a single item (matches the list-level delete pattern)
  verify: present ssConfirm src/kroger_mcp/web/templates/favorites_detail.html
- [x] ingredients.html removeIng() surfaces a failure instead of silently no-op'ing
  verify: present showMsg src/kroger_mcp/web/templates/ingredients.html
- [x] deals.html removeWatch() surfaces a failure instead of an unguarded, uncaught fetch
  verify: present r.ok src/kroger_mcp/web/templates/deals.html
- [x] pantry.html's borderline icon-only close/clear/cancel controls have accessible names
  verify: present aria-label src/kroger_mcp/web/templates/pantry.html

## Decisions
- [x] action_menu.html's `product` card_type has no delete/remove leaf at all (confirmed by reading the macro source, not just absence of a JS listener) — of the pages that render the shared action menu, only products.html lacked a delete listener, and it correctly doesn't need one. shopping_list.html/pantry.html don't use this macro at all. No code change was needed; this was verified, not assumed.
- [x] This stream's verification is code-presence based (see verify: annotations above); a live browser pass exercising deletePlan/safety.html's error path was not performed this session. If full UI confidence is wanted before shipping, run the dashboard locally and trigger each fixed action once (see "Manual verification" below).

## Manual verification (not yet run this session)
- Start the dev server, open `/meal-plan`, click delete on a plan, confirm the `ssConfirm` dialog appears and cancels/deletes correctly.
- On `/safety`, throttle or block a request in devtools and confirm an approve/block action now surfaces an error instead of silently no-op'ing.
