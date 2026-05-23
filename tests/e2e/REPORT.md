# Frontend Bug Finder — Run Report

**Target:** `http://127.0.0.1:8000` (Smart Shopper FastAPI dashboard, Jinja2 + Alpine.js)
**Date:** 2026-05-22
**Account mode:** Throwaway local user (provisioned via `/register`, prefix `__E2E__{runId}__`)
**Destructive scope:** Full — `__E2E__`-prefixed entities only

## Summary

| Metric | Count |
|---|---|
| HTML routes scanned | 14 |
| API endpoints exercised (destructive) | 9 |
| Specs generated | 9 files (31 tests) |
| **Tests passing** | **31** |
| Tests skipped | 0 |
| Tests failed (post auto-fix) | 0 |
| Auto-fixed bugs | 2 |
| Reported bugs | 0 |

## Auto-fixed

| # | Where | One-liner |
|---|---|---|
| 1 | `tests/e2e/products.spec.ts:14` | Wrong query param name — used `search_term`, OpenAPI says `q`. Endpoint correctly returns 400 with `{"error":"Provide a search term or category"}` on unknown params (good API hygiene). |
| 2 | `tests/e2e/recipes.spec.ts:25` | Initial selector targeted `input[type=number][name*=serving]` — the actual stepper uses Alpine `@click="inc()" / "dec()"` on plain `<button>` elements inside a stepper pill, and ingredients render in `<div data-ingredient-row>` rather than `<li>`. Selector tightened to `[data-ingredient-row]` + `button` filtered to literal `+`. |

Both are spec-side mechanical fixes — no source change.

## Reported

**None.** No P0/P1/P2/P3 bugs surfaced.

## Coverage

### HTML routes (smoke + console-error + 5xx-detector)
`/`, `/dashboard`, `/recipes`, `/meal-plan`, `/favorites`, `/pantry`, `/products`, `/shopping-list`, `/deals`, `/safety`, `/ingredients`, `/settings`, `/login`, `/register` — all 200, no console errors, no 5xx network responses.

### Auth (`auth.spec.ts`)
- Register validation: password mismatch, password length, duplicate email — all return 400 with the expected error message.
- Login validation: wrong password → 401; unknown email → 401; happy path → cookie set, redirect off `/login`.

### Destructive CRUD (API-level, `__E2E__` prefix)
- `POST/DELETE /api/favorites/lists` — round-trip clean, list also appears + disappears in `/favorites` UI.
- `POST/DELETE /api/ingredients/custom` — round-trip clean.
- `POST/DELETE /api/shopping-list/items` — round-trip clean.
- `POST/DELETE /api/safety/approved` — round-trip clean.

### UI workflows
- `recipes.spec.ts`: list → detail navigation, ingredient + instruction sections present, **servings stepper actually re-renders ingredient quantities** (validates the `feat(web): inline-edit recipe detail page + fix ingredient scaling` commit).
- `products.spec.ts`: search input visible; `GET /api/products/search?q=milk` returns 2xx with results.
- `workflows/navigation.spec.ts`: every nav `<a>` reachable from `/dashboard` resolves to 2xx.

## Notes & observations (not bugs)

1. **Auth middleware is commented out** in `src/kroger_mcp/web/app.py:95` (`# app.add_middleware(AuthMiddleware)`). Every dashboard route is reachable anonymously today. If this is intentional (single-user local app), no action; if it should enforce login post-MVP, that's a future story, not a regression. Flagged so it doesn't surprise anyone.
2. The legacy `playwright.config.js` at repo root points at port 8080 and `tests/playwright/` (old debug scripts). The new suite uses an isolated `tests/e2e/playwright.config.ts` and is run with `--config=tests/e2e/playwright.config.ts` so the two don't collide.
3. The `tests/playwright/` folder contains debug `.js` scripts (not standard `@playwright/test` specs). Not touched; would benefit from a separate cleanup pass.

## How to re-run

```bash
# 1. Start the dev server on :8000 (you do this manually)
uvicorn kroger_mcp.web.app:app --reload --port 8000

# 2. Provision a fresh throwaway account (idempotent if account.json exists)
npx tsx tests/e2e/scripts/provision-account.ts

# 3. Run the full suite
npx playwright test --config=tests/e2e/playwright.config.ts

# 4. Tear down test data + remove account.json
npx tsx tests/e2e/scripts/teardown.ts
```

## Files produced

- `tests/e2e/playwright.config.ts` — isolated config with prod guardrail in `global-setup.ts`
- `tests/e2e/global-setup.ts` — refuses any host not containing `localhost / 127.0.0.1 / staging / dev / preview`
- `tests/e2e/fixtures.ts` — `authedPage` + `testUser` fixtures (cookie-based login)
- `tests/e2e/scripts/provision-account.ts` — drives `/register` via Playwright, writes gitignored `account.json`
- `tests/e2e/scripts/teardown.ts` — deletes `__E2E__`-prefixed entities, removes `account.json`
- `tests/e2e/_discovery/route-map.json` — static + introspected route map (gitignored runtime artifact)
- `tests/e2e/_discovery/account.json` — gitignored runtime creds
- 9 spec files / 31 tests
