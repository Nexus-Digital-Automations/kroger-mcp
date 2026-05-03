---
title: Recipe Detail "Add to List" button — wire up to shopping list
status: completed
created: 2026-05-03
---

## Vision
On the recipe detail page, the green "Add to List" button in the header
currently only smooth-scrolls to the ingredients card and never sends any
request. Users click it and nothing happens. The backend endpoint
`POST /api/shopping-list/add-recipe` and the Alpine method
`ingredientPanel.addToShoppingList()` already exist — they just aren't
wired to a clickable trigger or to any visible feedback.

This spec wires the button to the existing endpoint, surfaces inline
status feedback so success and failure are unmistakable, and respects
the current servings stepper value.

## Requirements
- The header "Add to List" button must trigger
  `POST /api/shopping-list/add-recipe` with the current recipe id and
  the servings value selected on the ingredients card stepper.
- While the request is in flight, the button must show a "Adding…"
  state and be disabled (no double submits — `window.api`'s dedupe is
  the second line of defense, not the first).
- On success, the button must show inline confirmation
  ("Added · N items") for ~3 seconds before reverting.
- On failure, the button must show an inline error tag
  ("Failed — &lt;reason&gt;") for ~5 seconds before reverting.
- Servings selected on the ingredients-card stepper must be sent as
  `servings_override` so quantities reflect what the user sees.
- No regression to the existing "+ Meal Plan" or "Edit" buttons in the
  same header row.

## Acceptance Criteria
- [x] Clicking the header "Add to List" button issues a single
  `POST /api/shopping-list/add-recipe` with body
  `{recipe_id: <id>, servings_override: <stepper value>}`. Verified
  via Chrome DevTools MCP network panel.
- [x] Response body's `items_added` is reflected in the inline success
  label on the button.
- [x] Rapid double-click produces exactly one network request (Alpine
  `disabled` + `window.api` dedupe). Verified via DevTools network log.
- [x] If the endpoint returns 4xx/5xx, the button shows
  "Failed — &lt;message&gt;" without throwing an uncaught error in
  the JS console.
- [x] No console errors on page load or on click.
- [x] `addToShoppingList()` no longer references raw `fetch()` — it
  routes through `window.api.post` for consistency with the rest of
  the web UI.
- [x] `ruff check src/` exits 0.
- [x] Playwright + Chrome DevTools MCP run: navigate to a recipe,
  click "Add to List", confirm the new item appears on
  `/shopping-list` page.

## Technical Decisions
- **Event-bridge pattern.** The button sits in the page-header card,
  outside the `ingredientPanel` Alpine scope that owns `servings` and
  `addToShoppingList()`. Rather than duplicate state, the button
  dispatches a `recipe-add-to-list` `CustomEvent` on `document`, and
  `ingredientPanel` listens via Alpine's `@recipe-add-to-list.document`
  modifier and forwards to `addToShoppingList()`. This mirrors the
  existing "+ Meal Plan" / `open-meal-panel` pattern in the same row.
- **Inline feedback over global toast.** A page-wide toast host does
  not exist yet (api-client.js dispatches `toast:show` but nothing
  catches it). Adding one is a separate concern; this spec keeps
  feedback localized to the button via existing `adding` / `addDone`
  / `addErr` state — they were defined but never rendered.
- **Promote to `window.api`.** The existing `addToShoppingList()`
  uses raw `fetch`. Since we're modifying it anyway, promote to the
  shared client so dedupe applies and error toasts (when the host
  ever lands) work uniformly. Boy scout fix.

## Progress
- 2026-05-03 — Implemented via Alpine.store('addRecipe') bridge; removed
  dead `addToShoppingList()` from `ingredientPanel`. Verified end-to-end
  with Chrome DevTools MCP on `/recipes/0347404a`:
  - Single `POST /api/shopping-list/add-recipe` per click (dedupe of
    rapid double-click confirmed via network panel — exactly 1 request).
  - Response `items_added: 21` rendered as "Added · 21 items" on the
    button; reverted after 3s.
  - 19 items appeared on `/shopping-list` (delta after consolidation
    against 21 pre-existing items).
  - No new console errors. Pre-existing favicon 404 + Tailwind CDN
    advisory unchanged.
  - `ruff check src/` exits 0.
