---
title: Frontend API Client and Request Dedupe
status: planning
created: 2026-04-24
---

## Vision
Replace 20+ scattered `fetch('/api/...')` call sites with one tiny client that handles JSON encoding, error toasts, loading state hooks, and dedupes concurrent identical requests.

## Requirements
1. New `src/kroger_mcp/web/static/js/api-client.js` exposing `window.api.{get,post,patch,delete}`.
2. Wired into `base.html` before page-level scripts (so every Alpine `x-data` factory can use it without imports).
3. Concurrent identical requests (same method+path+body) share a single in-flight `Promise`.
4. Centralized error toast surfaced via the existing Alpine custom-event bus pattern (`@toast:show.window`).
5. Migrate every `fetch('/api/...')` in `web/templates/*.html` to use the client.
6. Replace `window.prompt()` in `products.html` (favorites list creation) with the existing `.ss-modal-card` modal pattern.

## Acceptance Criteria
- [ ] `src/kroger_mcp/web/static/js/api-client.js` exists with JSDoc header and ≤120 lines
- [ ] `grep -rn "fetch('/api/" src/kroger_mcp/web/templates/` returns 0 lines
- [ ] `grep -rn "window.prompt" src/kroger_mcp/web/templates/` returns 0 lines
- [ ] `tests/playwright/test_api_client.js` passes (happy path, error toast, dedupe assertion)
- [ ] Manual: rapid-click "Add to Cart" 5× sends 1 server request (verified via dev tools network panel)
- [ ] Existing `tests/playwright/test_all_features.js` and `test_all_buttons.js` still pass
- [ ] No new `console.error` from any page on initial load
- [ ] Lint (Phase 1) still green

## Technical Decisions
- **`window.api`, not ES modules** — codebase has no bundler and uses script tags. Adding modules would require build tooling out of scope.
- **Dedupe key includes body** — different POST bodies for the same path are independent operations; only true duplicates collapse.
- **Dedupe is best-effort** — TTL is the lifetime of the in-flight promise. Once resolved, a fresh identical request will fire (this is correct behavior for retries).
- **Error toast event name `toast:show`** — Alpine `$dispatch('toast:show', {message, level})` matches the existing `action-menu:*` event-bus precedent.
- **Modal for favorites name** — reuse `.ss-modal-card`; add an Alpine `x-data` modal in `products.html` with `name` field, "Cancel" / "Create" buttons.

## Progress
- [ ] Spec approved
- [ ] api-client.js + base.html wiring
- [ ] Toast event handler
- [ ] Page template migrations
- [ ] Modal replacement for window.prompt
- [ ] Playwright dedupe test
- [ ] Verification
