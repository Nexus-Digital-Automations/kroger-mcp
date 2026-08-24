# Verify the Smart Shopper MCP builds, runs, and supports its full feature surface

## Context

The user asked to confirm the Smart Shopper MCP is "built and working", "supports all
the features", and that the prod Mac mini instance can work.

Baseline established at the start of this session (evidence, not assumption):

- **Prod services up**: `com.smartshopper.web` PID 1500 serving port 8000 (HTTP 302),
  `homebrew.mxcl.postgresql@16` on 5433, `homebrew.mxcl.redis` (`PONG`),
  `com.user.kroger-discount-scanner` loaded.
- **Code in sync**: prod `~/kroger-mcp` md5s for `server.py`, `safety_tools.py`,
  `recipe_tools.py` match the local `main` working tree byte-for-byte.
- **Local build clean**: `uv sync --frozen` resolves 83 packages; `create_server()`
  imports and registers **18 tools**.
- **Feature surface measured**: those 18 tools expose **156 distinct `action` values**
  (auth 6, cart 7, deals 6, favorites 14, guides 6, info 10, ingredients 10,
  location 6, meal_plan 14, notion 7, pantry 9, predictions 15, privacy 4,
  products 7, recipes 10, reports 5, safety 15, shopping_list 5).
- **MCP transport**: `.mcp.json` runs the server on prod over SSH via
  `/Users/macmini1/.config/mcp/kroger-run.sh` (`uv run --frozen kroger-mcp`).
  A mid-session disconnect occurred; re-running that script over SSH brought
  FastMCP 3.3.1 up cleanly, so the drop was transport-transient, not a server fault.

"Working" therefore cannot be claimed from "it starts". The only honest way to claim
full feature support is to invoke every one of the 156 actions against the real prod
server and record what each returns.

## Design

Build `scripts/smoke_mcp.py`: an MCP stdio client that speaks to the **production**
server over the exact `.mcp.json` transport (ssh → `kroger-run.sh`), invokes every
action with safe arguments, and records one JSONL row per action.

Safety model (user-selected: preview/dry-run only):

- Actions are classified `read` / `preview` / `skip`.
- `read` — pure queries, invoked for real.
- `preview` — write-capable but supports a non-committing mode
  (`confirm=False`, `preview_only=True`, `mark_as_viewed=False`); invoked in that mode.
- `skip` — no non-committing mode exists (e.g. `cart.clear`, `privacy.delete_my_data`,
  `ingredients.reset_to_default`, `auth.force_reauth`). NOT invoked. Every skip is
  recorded with its reason and reported, never silently dropped.

A row is a PASS if the call returns a structured response the tool itself considers
well-formed. Rows that raise, time out, or return `success: false` for a
non-user-error reason are FAILs and get triaged individually.

Resumability: results append to `output/mcp-smoke/results.jsonl` after each call, so
an interrupted run resumes from the last completed action rather than restarting.

Deliverable beyond the report: the same script doubles as the repeatable health check,
plus `docs/prod-runbook.md` documenting how to verify/restore the prod mini.

## Decisions

- [x] Smoke-test against the **production** MCP over the real `.mcp.json` SSH transport, not a local in-process server — that is the path the user actually uses, and a local run would not prove prod works.
  verify: present "\.mcp\.json" scripts/smoke_mcp.py — build_transport() reads the real config, so the test uses the production path
- [x] Write-capable actions are exercised in preview/dry-run mode only; no live cart, pantry, recipe, or consent mutations.
  verify: present "preview_only" scripts/smoke_mcp_spec.py
- [x] Actions with no non-committing mode are skipped and reported explicitly with a reason, never counted as passes.
  verify: cmd uv run --frozen python -c "import sys; sys.path.insert(0,'scripts'); from smoke_mcp_spec import SPEC; bad=[(t,a) for t,a_ in SPEC.items() for a,(m,_,w) in a_.items() if m=='skip' and not w]; assert not bad, bad; print('all 65 skips carry a reason')"
- [x] The smoke harness is checkpointed to JSONL after every call so an interrupted run resumes instead of re-running the whole 156-action matrix.
  verify: present "handle.flush\(\)" scripts/smoke_mcp.py — append-then-flush per row; resume confirmed live (156 rows before, 156 after, nothing re-executed)

## Acceptance Criteria

- [x] `scripts/smoke_mcp.py` exists and connects to the prod MCP over the `.mcp.json` transport.
  verify: present "def build_transport" scripts/smoke_mcp.py
- [x] All 18 registered tools and all 156 actions are enumerated from the live server, and the harness's coverage table accounts for every one (invoked or explicitly skipped with a reason).
  verify: cmd uv run --frozen python -c "import sys; sys.path.insert(0,'scripts'); from smoke_mcp_spec import SPEC; n=sum(len(v) for v in SPEC.values()); assert (len(SPEC),n)==(18,156), (len(SPEC),n); print('18 tools / 156 actions')"
- [x] A results report is written to `output/mcp-smoke/` listing per-action PASS / FAIL / SKIP with the reason for each non-pass.
  verify: present "Per-tool results" output/mcp-smoke/report.md
- [x] Every FAIL is either fixed or documented with a root cause; no failure is left unexplained.
  verify: absent "ON fli\.product_id = pi\.product_id$" src/kroger_mcp/analytics/favorites.py — every pantry join is user-scoped; the remaining 5 FAILs are re-auth + missing NOTION_API_KEY, both documented in the report
- [x] `docs/prod-runbook.md` documents the verified prod topology and the restore procedure.
  verify: present "Restore procedures" docs/prod-runbook.md

## Tasks

- [x] Build `scripts/smoke_mcp.py` with the action-argument spec table and read/preview/skip classification.
- [x] Run the full 156-action smoke test against prod.
- [x] Triage every FAIL to a root cause; fix the ones that are real defects.
- [x] Write the results report to `output/mcp-smoke/`.
- [x] Write `docs/prod-runbook.md`.

## Outcome

**82 PASS / 5 FAIL / 69 SKIP** across 156 actions. Effective coverage is 86 exercised —
the 4 fixture-gated skips (`deals.get_price_history`, `predictions.get_item_stats`,
`get_history`, `explain_recommendation`) were probed directly against prod with a real
product ID and returned cleanly.

Four live-only defects were found and fixed, three of them the same SQLite→Postgres
portability class that passes the test suite and fails only on prod:

1. `deal_tools.get_latest_scan` — bare `scan_time` under `GROUP BY` (`c81605a`)
2. `analytics/favorites.py` — five unscoped `pantry_items` joins, a cross-tenant leak,
   plus the same bare-column error (`c81605a`, `efc14e9`)
3. `product_tools.py` — `client.search_products` → `client.product.search_products`,
   which made `scan_for_whole_foods` raise on every call (`efc14e9`)
4. `deal_tools.get_price_history` — `MAX(on_sale)` over a `BOOLEAN`; Postgres has no
   `max(boolean)` (`18b1915`)

Also fixed: the MCP tool and the web settings route each hardcoded their own OAuth scope
string and both omitted `profile.compact`. They now share `KROGER_OAUTH_SCOPES`, pinned
by `tests/test_oauth_scopes.py`.

The 5 remaining failures are not code defects — 3× `auth.*` awaiting interactive re-auth,
2× `notion.*` awaiting `NOTION_API_KEY`.

### Follow-on: the scoping guard, and what writing it kept finding

Locking defect 2 in with a regression test (`tests/test_user_scoping_contract.py`) turned
into its own thread, because every time the guard was widened it found another live leak
that the smoke sweep had passed:

- `prediction_tools._build_recommendation_context` and `categories.get_items_by_category` —
  both read another tenant's `product_statistics` (`c028f0c`)
- `ingredient_management_tools.py:974` (`ingredients.preview_impact`) — joined
  `purchase_events` with no user filter anywhere in the statement (`5313b95`), found only
  after an audit **falsified the stated rationale** for checking `LEFT JOIN` only
- the guard itself then failed **four consecutive adversarial reviews**, each finding a
  real defect: it accepted a *mention* of `user_id` (`GROUP BY pe.user_id`) as a filter and
  its character window spilled past the SQL literal into the next function (`7171cfc`); the
  `ast` rewrite dropped module-level SQL (`dd73212`); the f-string fix hid SQL nested in an
  interpolation (`4ac4810`); and the `RIGHT`/`FULL` rule was **semantically inverted** while
  a following join's qualifier was swallowed by the previous `ON` clause (`c65310d`). The
  last of those was fixed too aggressively and let a subquery's `WHERE` scope an outer
  preserved join, caught by diffing old-vs-new catch sets (`dc5beb5`).

Two lessons, both demonstrated rather than argued:

1. **A smoke PASS is not evidence a query is correctly scoped** — a leaking query returns a
   perfectly well-formed response. Two live leaks passed the sweep while leaking.
2. **This guard was wrong in every round it was attacked and in none where it was read.** A
   blind-spot list asserts *absences*, which reading a regex cannot confirm; only executing
   the case can. Corollary: a *shrinking* guard is more dangerous than a growing one, so
   each change is now checked by diffing the old and new catch sets, not by reviewing the
   new code.

Three items are open and deliberately not closed here, each needing a decision rather than
a fix: `recipes` has `user_id` on Postgres but not in SQLite at all; `auto_categorize_all`
mixes every user's statistics into the shared `products.category_type` column; and 43 prod
`purchase_events` rows carry `user_id IS NULL` (a pre-migration backlog) that the
`preview_impact` fix now correctly excludes. Detail in `output/mcp-smoke/report.md`.
