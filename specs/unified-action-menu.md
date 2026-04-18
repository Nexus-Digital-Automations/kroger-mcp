---
title: Unified Layered Action Menu for Cards
status: active
created: 2026-04-17
---

## Vision

Replace every scattered action button and popup on product cards, favorites-detail items, recipe cards, and shopping-list rows with a single standardized "Actions" dropdown. The dropdown is a layered/cascading menu so picking a specific favorites list, recipe, or meal-plan slot is a single interaction. One source of truth (shared Jinja macro + Alpine component + CSS) replaces the inline per-template implementations currently in products.html, recipes.html, favorites.html, and shopping_list.html.

## Requirements

### R1. Unified component
- One Jinja macro renders the Actions button and its menu DOM.
- One Alpine component (registered globally) handles open/close, submenu state, cascading hover-intent, keyboard nav, click-away, and focus management.
- One CSS file defines desktop flyout vs. mobile accordion layout, toggled via a `data-mode` attribute on the root.

### R2. Card coverage
- Product cards on `/products` and `/deals`
- Item rows on `/favorites/{list_id}`
- Recipe cards on `/recipes`
- Rows on `/shopping-list`

### R3. Menu contents per card
- **Product card:** Add to Favorites ▸ [lists…, + New List] · Add to Shopping List · Add to Recipe ▸ [recipes…] · View Details
- **Favorites-detail item:** Move to List ▸ [other lists] · Add to Shopping List · Add to Recipe ▸ [recipes…] · Remove
- **Recipe card:** Add to Shopping List · Add to Meal Plan ▸ [plan ▸ date+slot] · Edit · Delete
- **Shopping-list row:** Save to Favorites ▸ [lists…] · Remove (qty +/- remains inline)

### R4. New capability
- `POST /api/recipes/{recipe_id}/ingredients` adds a product (by id + description) as an ingredient to an existing recipe. Enables "Add to Recipe" on product cards.

### R5. Interaction model
- Desktop (hover-capable viewport ≥768px): cascading flyout. Submenu opens on hover of its trigger with a ~180ms close delay (hover-intent). Arrow-right also opens. Click on trigger also opens.
- Mobile (non-hover or <768px): accordion drill-down. Tap replaces menu content with the submenu; a back button returns.
- Mode is set from `matchMedia('(hover: hover) and (min-width: 768px)')` and updates on resize.

### R6. Accessibility (hard requirement)
- Trigger: `aria-haspopup="menu"` with live `aria-expanded`.
- Menu root: `role="menu"`. Leaves: `role="menuitem"`. Submenu triggers also `role="menuitem"` with `aria-haspopup="menu"` + `aria-expanded`.
- Keyboard: ArrowDown/ArrowUp traverses menuitems at the current level; ArrowRight opens submenu and focuses its first menuitem; ArrowLeft/Escape goes back (or closes at root); Enter/Space activates a leaf; Home/End jump to first/last; focus returns to the trigger when the menu closes.
- Click-away closes the menu but does not fire from clicks inside any nested submenu.

### R7. Data freshness
- After "+ New List" succeeds, host pages refetch `/api/favorites/lists` and update their reactive list; open menus close so the next open shows fresh data.

### R8. Primary actions inside menu
- No primary action (e.g., `+ Cart`, `Delete`) remains visible outside the menu. All actions are leaves inside the menu.
- Exception: the shopping-list qty +/- buttons stay inline because they are rapid-fire repeat actions, not discrete menu choices.

### R9. Regression safety
- Existing Playwright test suite `tests/playwright/test_all_buttons.js` (121 tests) must continue to pass. Preserve existing leaf labels (`+ Cart`, `Save`, `Remove`) so text locators still resolve.
- New tests in `tests/playwright/test_action_menu.js` cover cascade behavior.

## Acceptance Criteria

- [x] Shared Jinja macro `_macros/action_menu.html` exists and is included from each of the 5 affected templates.
- [x] Shared Alpine component `Alpine.data('actionMenu', …)` is registered once (served from `/static/js/action_menu.js`) and is the only implementation used by every card.
- [x] `/static/` is mounted in `app.py` and the JS+CSS load from `base.html`.
- [x] FastAPI helper `action_menu_context(request)` returns `{favorites_lists, recipes, meal_plans}` and is called by the 5 page route handlers.
- [x] `POST /api/recipes/{recipe_id}/ingredients` accepts `{product_id, description, quantity?, unit?}` and appends to `recipes[recipe_id].ingredients`; returns 404 when the recipe is missing.
- [x] Every card listed in R2 has exactly one visible "Actions" button and zero other action buttons (qty +/- on shopping list excepted).
- [x] Every submenu trigger has `aria-haspopup="menu"` and an `aria-expanded` attribute that toggles with state.
- [x] Keyboard traversal works: a user can reach every leaf from every card type using only the keyboard, starting from the Actions button.
- [x] Focus returns to the trigger when the menu closes (verified in at least one Playwright test).
- [x] Mobile accordion (simulated at 375×812) shows a Back button inside any open submenu and returns to the parent level on tap.
- [x] Existing `test_all_buttons.js` runs green after the helper swap.
- [x] New `test_action_menu.js` runs green covering: keyboard traversal, hover-intent delay, mobile accordion back, `aria-expanded` toggle, focus return, `+ New List` refresh, 3-level Meal Plan cascade.
- [x] Playwright MCP golden path (see plan verification section) passes: shopping list → save to favorites list → toast; product → + Cart via menu; product → Add to Recipe → ingredient appears on recipe detail; recipe → 3-level Meal Plan cascade opens existing slide-out panel; favorites-detail → Move to List moves the item.

## Technical Decisions

- **No new framework.** Keep Alpine.js + Tailwind; no React, Radix, or shadcn. Project has no build step and adding one is out of scope.
- **Component distribution via static files**, not inline per template. Requires mounting `/static/`, which is currently absent.
- **Context injection via a helper function**, not middleware or Flask-style context processors (which FastAPI lacks). Each of the 5 page handlers calls `action_menu_context(request)` explicitly.
- **Event-based network calls.** The menu dispatches `CustomEvent('action-menu:<action>', {detail})` on its root so the existing host Alpine components (e.g., `productBrowser`, `shoppingListData`) continue to own the actual network calls. This preserves the 121 test selectors.
- **Migration order** (each step independently shippable): shopping-list → favorites-detail → product (products, deals) → recipe. Shopping list is smallest and validates the whole pattern end to end.

## Progress

- [x] Spec drafted
- [x] Foundation (macro, Alpine component, CSS, context loader, static mount)
- [x] Shopping-list migration
- [x] Favorites-detail migration
- [x] Product cards migration
- [x] `POST /api/recipes/{id}/ingredients` endpoint
- [x] Recipe card migration (with 3-level Meal Plan cascade)
- [x] test_all_buttons.js updated (120 passed, 0 failed)
- [x] test_action_menu.js authored (23 passed, 0 failed)
- [x] Playwright MCP golden-path verification (2026-04-18)
