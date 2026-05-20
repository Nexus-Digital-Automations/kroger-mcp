---
title: Full frontend audit — user-journey tests, bug fixes, feature backlog
status: active
created: 2026-05-03
---

## Vision
Walk every primary user-facing page in the web UI, exercise every
visible button/input/toggle through a real user journey, log and fix
every bug found, and produce a prioritized feature backlog written
from a user's perspective.

## Approval Trail
- 2026-05-03 ~17:15 CDT — user requested:
  "go through the whole frontend, make tests for every button/feature
   using playwright/devtools through the frontend, then identify any
   bugs/features and make fixes. also come up with an md file of
   potential features/things we should add. think like a user."
- ~17:16 CDT — Claude posed 4 clarifying questions via AskUserQuestion.
- ~17:17 CDT — user picked: full user journeys; fix every bug found;
  user-job framing for the feature doc; cover Recipes+ShoppingList+Cart
  / Pantry+MealPlan+MealTracker / Settings+Auth+Safety. Favorites,
  analytics, predictions out of scope for this pass.

## Scope (in / out)

In scope (audit these pages, with deep journeys):
  1. Recipes (`/recipes`) + Recipe Detail (`/recipes/<id>`)
  2. Shopping List (`/shopping-list`)
  3. Cart (`/cart`)
  4. Pantry (`/pantry`)
  5. Meal Plan (`/meal-plan`)
  6. Meal Tracker (`/meal-tracker`)
  7. Settings (`/settings`)
  8. Login / auth handshake (`/login`)
  9. Safety + Ingredients (`/safety`, `/ingredients`)

Out of scope this pass: Favorites, Analytics, Deals, Predictions,
Chat widget. (Already partially covered by `test_all_features.js`.)

## Requirements

### A. Tests — full user journeys
- New file `tests/playwright/test_full_journeys.js` — one journey per
  in-scope page (or one journey per cohesive workflow that spans pages).
- Each journey:
  - Boots the running dev server (port 8000).
  - Navigates to the page.
  - Exercises every interactive element (button, input, toggle,
    select, link with side-effects) at least once.
  - Asserts on observable outcomes — DOM changes, network requests,
    persisted state via `/api/...` checks.
  - Cleans up any state it mutates so re-runs are idempotent.
- Total runtime budget: ≤ 10 minutes for the full file.

### B. Bug fixes
- Every bug surfaced by the audit OR by Chrome DevTools MCP exploration
  is fixed in the same effort, in scope-appropriate commits.
- A bug is anything that violates the visible promise of the UI:
  - Click that does nothing.
  - API call that 4xx/5xxs and isn't surfaced.
  - Form that doesn't persist.
  - Console error on a normal page load.
  - Data that doesn't match server state.
- Each fix lands as its own commit with the bug clearly described.
- A running list lives at `docs/audit-bugs-2026-05-03.md` so reviewers
  can see what was found vs. fixed.

### C. Feature backlog
- New file `docs/feature-ideas-2026-05-03.md`.
- Grouped by user job (planning meals, shopping smart, tracking,
  health, etc.).
- Each idea has: who-it's-for, why-they'd-want-it, rough effort
  (S/M/L), priority hint (P0/P1/P2), and a one-line "what changes in
  the UI" sketch.
- At least 15 ideas, derived from gaps spotted during the audit and
  from "what would a Kroger MCP user actually wish existed?"

## Acceptance Criteria
- [ ] `tests/playwright/test_full_journeys.js` exists, runs against
  the live dev server, and exits 0.
- [ ] Every in-scope page has at least one journey covering its
  primary interactive elements.
- [ ] Every bug found is either fixed (committed) or — if it was
  outside the safe-to-fix envelope — explicitly documented as
  deferred in `docs/audit-bugs-2026-05-03.md` with a rationale.
- [ ] `docs/audit-bugs-2026-05-03.md` lists each bug with: where it
  was found, severity, fix commit (or "deferred + reason").
- [ ] `docs/feature-ideas-2026-05-03.md` exists with ≥15 ideas,
  grouped by user job, each with effort + priority + UI sketch.
- [ ] `ruff check src/` exits 0 after every commit.
- [ ] All existing tests still pass (`test_all_features.js` 76+,
  `test_user_flows.js` 27+).
- [ ] No new console errors introduced on any audited page.

## Technical Decisions
- **Per-page journey, single file.** One file keeps related setup/
  teardown together and lets the file budget runtime. If it grows past
  ~600 lines or 10 min, split per area.
- **Idempotent journeys.** Each journey snapshots pre-state via API,
  exercises the UI, then either restores state (DELETE additions) or
  uses a sentinel value the journey can identify and remove. No
  reliance on test ordering.
- **No mocks against the Kroger sandbox.** The cart-confirm path is
  excluded from auto-runs (covered by `test_all_features.js` modal-
  cancel test). Confirm-against-Kroger is a manual smoke only.
- **Bug list is a working doc, not a spec.** It lives in `docs/`,
  not `specs/`, because it's a snapshot of one audit pass — not a
  long-lived requirements artifact.

## Progress
