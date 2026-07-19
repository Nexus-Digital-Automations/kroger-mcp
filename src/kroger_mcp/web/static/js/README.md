# `static/js/` — frontend Alpine.js components

Plain, framework-free JS (no bundler) loaded directly via `<script src="...">`
tags from `base.html` (global, every page) or from an individual page
template (page-specific). Each file's own header comment is the source of
truth for its exact ownership/API; this is a one-line-per-file index so a
new file is discoverable without grepping the directory.

| File | Purpose |
|---|---|
| `api-client.js` | Shared `window.api` fetch wrapper — auto JSON-encode, `toast:show` on error, dedupes concurrent identical requests. Loaded globally. |
| `action_menu.js` (+ `.min.js`) | Unified dropdown action menu (product/recipe/favorites/shopping-list cards) — interaction state only, dispatches events for hosts to handle the actual network calls. Loaded globally. |
| `notifications.js` | The notification bell (`notifBell`) — polls `GET /api/notifications`, renders favorite-on-sale alerts / meals-to-confirm / pantry alerts / next-week-plan reminder, acts on each inline. Loaded globally. |
| `priority_feed.js` | The dashboard's "Needs your attention" feed (`priorityFeed`) — fetches the same `/api/notifications` payload as the bell (so the two never disagree) plus server-seeded overdue-favorites/smart-suggestions, groups everything into Critical/This week/Plan ahead tiers, and reuses the bell's exact action endpoints. Loaded from `dashboard.html` only. |
| `sort_rank.js` | Shared draggable multi-rank sort mixin (recipes, deals, products pages) — sort-dialog state, persistence, tiebreaker semantics. Loaded globally. |
| `ingredient_linker.js` | Tiny shared helper for the recipe ingredient "your usuals" suggestion call (`GET /api/ingredients/suggest`). Loaded globally. |
| `ingredient_panel.js` (+ `.min.js`) | The recipe Ingredients card (`ingredientPanel`) — ingredient table, servings stepper, inline edit/save. Shared by `recipe_view.html`/`recipe_edit.html`. |
| `linker_popover.js` (+ `.min.js`) | Kroger product-linking popover, an Alpine mixin spread into `ingredientPanel` (split out of `ingredient_panel.js` for file size, not a separate component). |
| `vendor/` | Third-party (Alpine.js). |

`.min.js` files are committed build output of the matching `.js` source —
never hand-edit a `.min.js` directly.
