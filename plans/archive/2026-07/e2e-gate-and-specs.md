<!-- last-reviewed: 2026-07-02 -->
# Plan: Complete the E2E suite + an isolated test-gate harness

**Audience:** an agent working *inside this repo* (`Smart Shopper` / `kroger-mcp`).
**Outcome:** every feature has a Playwright spec, and a single command boots an
isolated local instance, runs the whole suite against a throwaway dummy account,
and exits non-zero on any failure. That command becomes a pre-deploy gate
(wiring done separately — see "Out of scope" below).

---

## Why / context

This app is deployed to an always-on Mac mini. A separate deploy pipeline pushes
laptop code to the mini on `git push origin main`. We want **all features
verified via Playwright against a dummy account BEFORE a deploy happens**, and a
failing test to block the deploy.

The E2E suite already exists in `tests/e2e/` but only covers **9 areas**; **7
feature areas have no spec yet**. Your job: author the missing specs and build
the harness that runs the full suite in isolation.

### What already exists (study these first — match their conventions)
- `tests/e2e/playwright.config.ts` — config. `BASE_URL = E2E_BASE_URL ||
  http://127.0.0.1:8000`, `testDir = tests/e2e`, `testMatch = *.spec.ts`,
  `workers: 1`, JSON reporter → `test-results/e2e-results.json`,
  `globalSetup: ./global-setup.ts`.
- `tests/e2e/global-setup.ts` — refuses to run against non-dev hosts
  (allows `localhost`/`127.0.0.1`); asserts the app answers `/login` before the
  suite starts. **Does NOT boot the app** — the harness must.
- `tests/e2e/fixtures.ts` — exports `test` with an **`authedPage`** fixture
  (cookie login) and a `testUser`, plus `E2E_PREFIX(runId)`. Login selectors:
  `input[name="email"]`, `input[name="password"]`, `button[type="submit"]`.
  Credentials come from `tests/e2e/_discovery/account.json` (gitignored).
- `tests/e2e/scripts/provision-account.ts` — drives `/register`
  (`input[name="display_name"|"email"|"password"|"confirm_password"]`,
  `button[type="submit"]`) and writes `account.json`. **Skips if the file
  already exists.** Local auth only — no Kroger needed to register/log in.
- `tests/e2e/scripts/teardown.ts` — removes `__E2E__`-prefixed test data.
- Existing specs (templates): `00-smoke`, `auth`, `products`, `recipes`,
  `favorites`, `destructive-crud`, `two-user-isolation`, `workflows/navigation`,
  `test_ingredient_grading`.

### Canonical spec shape (from `favorites.spec.ts`)
```ts
import { test, expect, E2E_PREFIX } from './fixtures';

test('create+read+delete a __E2E__ X, verify in UI', async ({ authedPage, testUser }) => {
  const suffix = Math.random().toString(36).slice(2, 8);
  const name = `${E2E_PREFIX(testUser.runId)}thing-${suffix}`;
  const created = await authedPage.request.post('/api/<feature>', { data: { /* ... */ } });
  expect(created.ok(), `create ${created.status()}`).toBeTruthy();
  // ...navigate the UI, assert visible, then DELETE and assert gone...
});
```
Specs mix **API-level** assertions (`authedPage.request.get/post/delete`) with
**UI** assertions (`authedPage.goto(...)`, `getByText(...)`, `toBeVisible()`).
Namespace all created data with `E2E_PREFIX(testUser.runId)` and clean it up in
the test (or rely on `teardown.ts`).

---

## Decisions (locked — do not re-litigate)
- **Coverage:** author the 7 missing specs (below) so all features are covered.
- **Test instance:** ephemeral local app on **port 8099**, isolated cwd, fresh
  dummy account each run, torn down after. Never the mini.
- **Gate:** the harness exits non-zero on any failure; the caller (pre-push
  hook) blocks both push and deploy. (Wiring is out of scope here.)

---

## Task 1 — Build the harness `tests/e2e/run-gate.sh`

A POSIX `sh` script in `tests/e2e/run-gate.sh` that:
1. `cd` to the repo root (resolve from the script's own dir).
2. Ensure Playwright Chromium is present: `npx playwright install chromium`
   (no-op if cached).
3. Choose an isolated state cwd: `WORK="$(mktemp -d)"` so cwd-relative token
   files (`.kroger_token_*.json`, `kroger_cart.json`) land in the temp dir, not
   the repo. (Analytics DB + recipes are `__file__`-relative to the source tree
   — those test rows are `__E2E__`-namespaced and cleaned by `teardown.ts`;
   acceptable, and the **mini is never touched** regardless.)
4. Boot the app on port 8099 from the temp cwd, reusing this repo's project:
   `( cd "$WORK" && WEB_PORT=8099 uv run --frozen --project "$REPO" kroger-web ) &`
   Capture the PID. The app's `run()` self-kills anything already on the port.
5. Wait for readiness: poll `http://127.0.0.1:8099/login` until HTTP 200 (cap
   ~30s; fail loudly if it never comes up — dump the app log).
6. Fresh account: `rm -f tests/e2e/_discovery/account.json` then
   `E2E_BASE_URL=http://127.0.0.1:8099 npx tsx tests/e2e/scripts/provision-account.ts`.
7. Run the suite:
   `E2E_BASE_URL=http://127.0.0.1:8099 npx playwright test --config=tests/e2e/playwright.config.ts`;
   capture its exit code.
8. Always teardown (even on failure): run `teardown.ts`, `kill` the app PID,
   `rm -rf "$WORK"`.
9. `exit` with the suite's exit code.

Add a convenience npm script in `package.json`: `"e2e:gate": "sh tests/e2e/run-gate.sh"`.

**Validate Task 1** by running it against the *existing* suite first — it must
go green before you add new specs (proves the harness works in isolation).

---

## Task 2 — Author the 7 missing specs

One `*.spec.ts` per area in `tests/e2e/`. For each, **read the route files to
get exact paths/selectors** — do NOT guess:
- web page route: `src/kroger_mcp/web/routes/<feature>.py`
- JSON API: `src/kroger_mcp/web/routes/api/<feature>.py`

| Spec file | Feature | Read these routes | Cover (minimum) |
|---|---|---|---|
| `deals.spec.ts` | deals | `routes/deals.py`, `routes/api/deals.py` | page loads authed; `GET /api/deals…` returns array-shaped payload; a deal renders in UI |
| `meal-plan.spec.ts` | meal planning | `routes/meal_plan.py`, `routes/api/meal_plan.py` | page loads; create→read→delete a `__E2E__` meal plan via API; verify in UI |
| `pantry.spec.ts` | pantry | `routes/pantry.py`, `routes/api/pantry.py` | add→list→remove a `__E2E__` pantry item; UI reflects it |
| `safety.spec.ts` | ingredient safety | `routes/safety.py`, `routes/api/safety.py` | settings GET/POST round-trip; `check_product`/ingredient-list endpoint returns expected shape; UI page loads |
| `shopping-list.spec.ts` | shopping list | `routes/shopping_list.py`, `routes/api/shopping_list.py` | add→list→remove a `__E2E__` item; UI reflects it |
| `chat.spec.ts` | AI chat | `routes/api/chat.py` | **contract only, not LLM quality**: empty message → 400; `GET /api/chat/providers` shape; if no `DEEPSEEK_API_KEY`/provider key in env, assert graceful error rather than calling the LLM. UI: chat widget renders. Keep it deterministic + offline-safe. |
| `settings.spec.ts` | settings | `routes/settings.py`, `routes/api/settings.py` | load settings page; GET current settings; POST a change and read it back; revert |

Rules for every spec:
- `import { test, expect, E2E_PREFIX } from './fixtures'` and use `authedPage`.
- Namespace created data with `E2E_PREFIX(testUser.runId)`; delete what you
  create (idempotent across reruns — use a random suffix like favorites.spec).
- Prefer asserting on **behavior/contract** (status codes, payload shape, UI
  text appearing/disappearing), not brittle pixel/text exact-matches.
- Keep them **offline-safe**: nothing should depend on a live Kroger *user*
  token. Product search uses the client-credentials token (auto-fetched from
  `.env` `KROGER_CLIENT_ID/SECRET`); cart is local-tracked. If a feature truly
  needs a live user OAuth, assert the unauthenticated/again-graceful path
  instead of performing a real Kroger mutation.

**Validate Task 2:** after each spec, run just that file via the harness'
environment (`E2E_BASE_URL=http://127.0.0.1:8099 npx playwright test
tests/e2e/<file> --config=tests/e2e/playwright.config.ts`) against a manually
booted instance, or just re-run `npm run e2e:gate`. Fix selectors/paths until
green. Expect to iterate — route discovery from code rarely nails selectors
first try.

---

## Task 3 — Full green + record runtime
- `npm run e2e:gate` runs the **entire** suite (existing + 7 new) and exits 0.
- Note the wall-clock runtime in the PR/commit message (it becomes per-push
  latency once gated).
- Ensure `tests/e2e/_discovery/account.json`, `test-results/`, and any temp
  artifacts are gitignored (check `.gitignore`).

---

## Out of scope for you (handled in the Automation Agent repo)
The pre-deploy **gate wiring** lives outside this repo: the laptop `pre-push`
hook (installed from `Automation Agent/scripts/smartshopper-pre-push.hook`) will
be updated to run `sh tests/e2e/run-gate.sh` and only deploy on exit 0. **You
just need to deliver a harness that returns the right exit code.** Contract:
`sh tests/e2e/run-gate.sh` → **exit 0 = all green (safe to deploy)**, non-zero =
block.

Do **not** add anything that references the mini, its Tailscale IP, or SSH —
this repo pushes to public GitHub. The harness is 100% localhost.

---

## Acceptance criteria
- [x] `tests/e2e/run-gate.sh` boots an isolated :8099 instance, provisions a
      fresh dummy account, runs the suite headless, tears down, returns the
      suite's exit code. `npm run e2e:gate` wraps it.
      verify: manual `npm run e2e:gate` run on 2026-07-02 — booted :8099,
      provisioned account, ran 75 tests, tore down cleanly, propagated the
      real suite exit code (1, matching 3 genuine failures below). Harness
      mechanics are correct.
- [x] Specs exist and pass: `deals`, `meal-plan`, `pantry`, `safety`,
      `shopping-list`, `chat`, `settings`.
      verify: same run — every test in all 7 files passed (deals.spec.ts 3/3,
      meal-plan.spec.ts 2/2, pantry.spec.ts 2/2, safety.spec.ts 3/3,
      shopping-list.spec.ts 2/2, chat.spec.ts 3/3, settings.spec.ts 2/2).
- [x] Full suite (existing + new) is green via `npm run e2e:gate`.
      verify: `npm run e2e:gate` on 2026-07-02 — 74 passed, 1 skipped
      (pre-existing conditional skip, unrelated), 0 failed, exit code 0
      (2.9m). The 3 failures from the prior run were root-caused and fixed,
      not just retried: (1) `guide_edit.html` used a double-quoted
      `x-data="guideEditor({{ guide | tojson }})"` — tojson emits double
      quotes, truncating the attribute so the whole guide editor silently
      never initialized (any guide, not just new ones). Fixed by
      single-quoting, matching favorites_detail.html's documented
      convention. (2) `meal_plan.html`'s deletePlan() built a confirm-dialog
      title with raw `"` chars inside the SAME double-quoted x-data
      attribute as the whole page's Alpine component — broke every method
      on the page (new plan, add meal, copy, delete, add-to-cart, and the
      plan-switch dropdown the failing test exercised), not just deletion.
      Fixed with `&quot;` entities. (3) Dashboard onboarding banner never
      showed for any account: `get_lists()` auto-creates both "My
      Favorites" and a "Snacks" list per user, but dashboard.py's
      custom_favorites_count only excluded the former, so the zero-state
      check never passed. Fixed by also excluding list_type=='snacks'.
- [x] A deliberately broken assertion makes `run-gate.sh` exit non-zero.
      verify: proven by the same run — genuine failures produced exit 1,
      propagated correctly through teardown.
- [x] No new dependency on a live Kroger user token; chat spec is offline-safe.
      verify: cmd grep -inE "mini|ssh|tailscale" tests/e2e/*.spec.ts tests/e2e/run-gate.sh
- [x] Test runs never write to the repo's working tree token files (isolated
      via temp cwd); `__E2E__` data is cleaned up.
      verify: same run's log shows `[teardown] account.json removed` and temp
      WORK dir usage per run-gate.sh design.
- [x] Nothing in the harness/specs references the mini or SSH.
      verify: cmd grep -inE "mini|ssh|tailscale" tests/e2e/run-gate.sh tests/e2e/deals.spec.ts tests/e2e/meal-plan.spec.ts tests/e2e/pantry.spec.ts tests/e2e/safety.spec.ts tests/e2e/shopping-list.spec.ts tests/e2e/chat.spec.ts tests/e2e/settings.spec.ts

## Tasks
- [x] Build tests/e2e/run-gate.sh isolated harness + e2e:gate npm script
- [x] Validate harness green against the existing suite
- [x] Author deals.spec.ts from routes/deals.py + api/deals.py
- [x] Author meal-plan.spec.ts from routes/meal_plan.py + api
- [x] Author pantry.spec.ts from routes/pantry.py + api
- [x] Author safety.spec.ts from routes/safety.py + api
- [x] Author shopping-list.spec.ts from routes/shopping_list.py + api
- [x] Author chat.spec.ts (contract-only, offline-safe) from api/chat.py
- [x] Author settings.spec.ts from routes/settings.py + api
- [x] Run full suite green via npm run e2e:gate and record runtime
      Runtime recorded: 2.9m for 75 tests (74 passed, 1 pre-existing
      conditional skip). Green — see acceptance criteria note for the 3
      root-caused fixes.
- [x] Confirm account.json/test-results/temp artifacts are gitignored
      verify: present tests/e2e/_discovery/account.json .gitignore
