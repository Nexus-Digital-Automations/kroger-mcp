---
title: Accessibility Pass — Landmarks, ARIA Labels, axe-core
status: planning
created: 2026-04-24
---

## Vision
Make the Smart Shopper UI reliably navigable by keyboard and screen readers. Establish an axe-core baseline so future regressions fail CI.

## Requirements
1. `base.html`: wrap structural regions in semantic landmarks — `<main>`, `<nav>`, `<header>`. Add a "Skip to main content" link as the first focusable element.
2. Add `aria-label` to every icon-only `<svg>` button (sidebar nav links, hamburger, search-clear, chat FAB, action-menu trigger). Decorative SVGs beside visible text get `aria-hidden="true"`.
3. Audit form inputs in `shopping_list.html`, `favorites.html`, `recipes.html`, `pantry.html` — every `<input>` either has an associated `<label>` or `aria-label`.
4. Integrate `axe-core` into Playwright via `tests/playwright/test_accessibility.js`. Page-by-page audit. Fail on `serious` or `critical` violations.
5. Document any consciously-accepted violations in this spec under "Technical Decisions".

## Acceptance Criteria
- [ ] `base.html` has exactly one `<main>` and one `<nav>` landmark
- [ ] Skip link is the first focusable element on every page
- [ ] `grep -E '<svg' src/kroger_mcp/web/templates/base.html` shows every interactive svg paired with `aria-label` or wrapping `<button aria-label=…>`
- [ ] `tests/playwright/test_accessibility.js` runs and reports 0 critical + 0 serious violations on: products, recipes, shopping_list, favorites, pantry, meal_planner
- [ ] Manual keyboard tab-through of products page: skip link works, focus visible on every interactive control, no traps
- [ ] All Phase 3 page changes (api-client migration) still functional
- [ ] Lint (Phase 1) still green

## Technical Decisions
- **axe-core via npm devDependency** — installed once, no CDN at test time. Bundle into Playwright via `await page.addScriptTag({ path: 'node_modules/axe-core/axe.min.js' })`.
- **Severity gate at `serious`** — `moderate` and `minor` would surface hundreds of color-contrast nits in the OKLCH palette. Address them in a follow-up.
- **Skip link CSS** — visually hidden until focused (existing `.sr-only` pattern if present, otherwise add `.ss-skip-link`).
- **Sidebar `<nav aria-label="Main">`** — disambiguates from any future secondary nav.

## Progress
- [ ] Spec approved
- [ ] Landmarks + skip link in base.html
- [ ] aria-label sweep
- [ ] Form-label audit
- [ ] axe-core integration + test
- [ ] Verification
