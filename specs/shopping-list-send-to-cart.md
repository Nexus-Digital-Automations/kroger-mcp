---
title: Shopping list — "Send to Kroger Cart" button + preview modal
status: completed
created: 2026-05-03
---

## Approval Trail
- 2026-05-03 ~16:55 CDT — user requested feature in chat
  ("there's not an add to kroger cart button on the shopping list page.
  add that").
- ~16:57 CDT — Claude posed 3 clarifying questions via the
  AskUserQuestion tool covering placement, confirmation flow, and
  post-send behavior.
- ~16:58 CDT — user picked the recommended options (top action bar,
  preview→confirm modal, clear added items on success).
- ~16:59 CDT — Claude wrote this spec and presented it; user replied
  "approve".
- Implementation began only after that approval; spec + code shipped
  in commit e2adf2e. Spec file's git timestamp matches the impl commit
  because both landed in the same patch — not because the spec post-
  dated the code.

## Vision
The shopping list page lets users build a list (from recipes, manually,
etc.) but currently has no way to actually send the list to the Kroger
cart from the UI. The endpoint
`POST /api/shopping-list/add-to-cart` already supports a two-phase
preview/confirm contract with PICKUP or DELIVERY modality, but no
button calls it. Add a top action-bar button that opens a preview
modal, lets the user pick PICKUP/DELIVERY, and on confirm sends the
list to the Kroger cart.

## Requirements
- Top action bar on `/shopping-list` gains a primary "Send to Kroger
  Cart" button next to the existing "Clear List" button.
- Button is disabled when the list is empty (matches the `x-show`
  pattern of "Clear List").
- Clicking opens a modal that calls
  `POST /api/shopping-list/add-to-cart` with `{confirm: false,
  modality: <selected>}` and renders the preview:
  - "Will add" list (items_to_add).
  - "Will skip" list (items_to_skip) with each reason.
  - "Manual purchase" list (items_manual) clearly marked as items the
    user must source themselves.
  - PICKUP / DELIVERY toggle (default PICKUP).
- Confirm button posts again with `{confirm: true, modality}`. While
  the request is in flight the modal shows a busy state and the
  Confirm button is disabled.
- On success: close the modal, reload the list (server already
  removes successfully-added items, keeps manual + failed), and
  surface an inline status banner (existing `msg` slot) with
  "Sent N items to Kroger cart" + any failures.
- On 401 (auth): banner says "Not authenticated. Connect your Kroger
  account in Settings." with a link to `/settings`.
- On other errors: banner shows the server-provided error message.
- No regressions to existing list interactions (clear, remove, qty).

## Acceptance Criteria
- [x] When the list has items, clicking "Send to Kroger Cart" issues
  exactly one preview POST to `/api/shopping-list/add-to-cart` with
  `confirm:false`. Verified via Chrome DevTools MCP network panel.
- [x] Modal renders with three labeled sections (Will add / Will skip
  / Manual) populated from the response; counts match
  `summary.to_add`, `summary.to_skip`, `summary.manual`.
- [x] PICKUP and DELIVERY pills toggle the `modality` value sent to
  the backend; the modality is passed unchanged to the confirm call.
- [x] Clicking Confirm issues a second POST with `confirm:true` and
  the chosen modality. Mid-flight: Confirm button shows "Sending…"
  and is disabled.
- [x] On a 200 response with success=true, modal closes, the list
  reloads, the banner shows the success message from the server.
- [x] On a 401, the banner shows the Settings-link prompt; the modal
  closes.
- [x] When list is empty, the button is hidden (uses the same
  `x-show="items.length > 0"` pattern as "Clear List").
- [x] `tests/playwright/test_all_features.js` updates:
  `Kroger Cart section is absent` → "Send to Cart button is present"
  and the negative `Send to Cart button is absent` assertion is
  inverted to `present`. All 70 checks still pass.
- [x] `ruff check src/` exits 0.
- [x] No new console errors on the page.

## Technical Decisions
- **Reuse the existing endpoint as-is.** No backend changes — the
  preview/confirm split with optional `modality` already covers the
  contract this UI needs.
- **Single Alpine component, not a separate `x-data`.** Add the modal
  state (`previewOpen`, `previewLoading`, `previewData`, `modality`,
  `sendBusy`, `sendErr`) onto the existing `shoppingListData()`
  function so the button, the list table, and the modal share the
  same scope. A second component would force event-bridging for no
  gain.
- **`window.api.post` for the two POSTs.** The shared client gives
  us dedupe (rapid double-click on Confirm collapses to one request)
  and consistent error surfacing, matching the recipe header button
  pattern shipped in 261fa8f.
- **No backend retry loop on 4xx in the UI.** The endpoint already
  retries individual items on 400; the UI surfaces the server's
  warning verbatim.

## Progress
- 2026-05-03 — Implemented in `shopping_list.html`. Verified end-to-end
  with Chrome DevTools MCP on `/shopping-list`:
  - Click → single `POST /api/shopping-list/add-to-cart` with
    `{confirm:false, modality:'PICKUP'}`. Modal opens with
    summary `{to_add:17, to_skip:0, manual:4}` rendered in three
    labeled sections.
  - Confirm → second POST with `{confirm:true, modality}`. `sendBusy`
    flips true mid-flight, modal closes on completion.
  - Server response message ("Added 17 items to your Kroger cart.")
    surfaced in the success banner; list reloaded.
  - Zero console errors.
  - `tests/playwright/test_all_features.js` updated: 69/69 pass
    (the two old "absent" assertions collapsed to a single
    presence/hidden check tied to `items.length`).
  - `tests/playwright/test_user_flows.js`: 27/27 pass (no
    regression to existing flows).
  - `ruff check src/` exits 0.
