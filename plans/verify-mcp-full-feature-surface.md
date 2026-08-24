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

- [ ] Smoke-test against the **production** MCP over the real `.mcp.json` SSH transport, not a local in-process server — that is the path the user actually uses, and a local run would not prove prod works.
- [ ] Write-capable actions are exercised in preview/dry-run mode only; no live cart, pantry, recipe, or consent mutations.
- [ ] Actions with no non-committing mode are skipped and reported explicitly with a reason, never counted as passes.
- [ ] The smoke harness is checkpointed to JSONL after every call so an interrupted run resumes instead of re-running the whole 156-action matrix.

## Acceptance Criteria

- [ ] `scripts/smoke_mcp.py` exists and connects to the prod MCP over the `.mcp.json` transport.
- [ ] All 18 registered tools and all 156 actions are enumerated from the live server, and the harness's coverage table accounts for every one (invoked or explicitly skipped with a reason).
- [ ] A results report is written to `output/mcp-smoke/` listing per-action PASS / FAIL / SKIP with the reason for each non-pass.
- [ ] Every FAIL is either fixed or documented with a root cause; no failure is left unexplained.
- [ ] `docs/prod-runbook.md` documents the verified prod topology and the restore procedure.

## Tasks

- [ ] Build `scripts/smoke_mcp.py` with the action-argument spec table and read/preview/skip classification.
- [ ] Run the full 156-action smoke test against prod.
- [ ] Triage every FAIL to a root cause; fix the ones that are real defects.
- [ ] Write the results report to `output/mcp-smoke/`.
- [ ] Write `docs/prod-runbook.md`.
