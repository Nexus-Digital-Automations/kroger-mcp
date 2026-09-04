# Gemma-powered seasonal draft selection (+ prod auto-approve off)

## Context

Follow-up to plans/auto-draft-auto-approve.md (complete). The user reversed one
piece and requested one feature:

1. **Auto-approve OFF for their account.** The code default was always 0; only
   the prod `user_settings` row for the live user (`a7fd322a…`) is 1 and must be
   reset to 0. (First ssh attempt timed out — the mini was unreachable.)
2. **Gemma picks the weekly dinners, seasonally.** User-locked decisions (from
   AskUserQuestion grilling, all Recommended options chosen):
   - **Runtime**: reuse the existing Google Gemma provider already in
     `web/chat_engine.py`'s `PROVIDER_REGISTRY` (`gemma4`: `gemma-4-31b-it` via
     the Google OpenAI-compat endpoint, `GEMINI_API_KEY`, free-of-charge tier —
     no new metered spend).
   - **LLM's job**: pick from SAVED recipes only. Gemma sees the saved-recipe
     catalog plus current date/season and upcoming holidays, and returns which
     N dinners fit best with a one-line reason each. It never invents recipes.
   - **Fallback**: ANY failure (no `GEMINI_API_KEY`, provider error, bad JSON,
     invalid recipe ids, wrong count) → silently fall back to the existing
     recency-rotation logic. The weekly workflow never breaks. The response is
     marked so the user can see whether Gemma was involved.

## Design

- **Dependency direction fix.** `analytics/` must not import from `web/`. Move
  `PROVIDER_REGISTRY`, `DEFAULT_PROVIDER`, `OpenAICompatibleClient`,
  `_client_cache`, `get_client`, `list_available_providers` from
  `web/chat_engine.py` into a new neutral module `src/kroger_mcp/llm_client.py`.
  `chat_engine.py` imports them back under the same names, so
  `web/routes/api/chat.py`, `tests/test_chat_stream.py`, and
  `tests/test_concurrency.py` keep working unchanged.
- **New `analytics/draft_selection.py`** (keeps meal_planning.py growth small,
  logic testable in isolation):
  - `season_for_month(month)` — month → "winter/spring/summer/fall" label.
  - `_chat_completion(messages)` — constructs `OpenAICompatibleClient("gemma4")`
    and returns `.chat(messages)`; the client's own missing-key/error handling
    applies (returns error dicts, never raises). This is the test seam.
  - `build_selection_prompt(...)` — strict-JSON instruction; includes today's
    date, season, upcoming holidays (`seasonal.get_upcoming_holidays` — pure
    calendar math, works with empty purchase history), the draft week's date
    range, recently-cooked recipe ids to avoid, and the recipe catalog
    (id, name, ingredient names).
  - `parse_selection(content, valid_ids, dinner_count)` — strips markdown
    fences, parses JSON, validates ids against the catalog, dedupes preserving
    order, truncates reasons; returns exactly `dinner_count` picks or `None`.
  - `select_dinners_with_llm(...)` — orchestrates the above; returns
    `list[{"recipe_id", "reason"}]` or `None`. Never raises to the caller.
- **`generate_draft` integration** (`analytics/meal_planning.py`): attempt the
  LLM selection inside try/except; on `None`/exception use the existing
  rotation sort. Persist per-meal reasons via `bulk_assign_meals` assignments'
  existing optional `notes` key. Response gains `selection_mode:
  "gemma" | "rotation"`; `get_attention`'s `weekly_draft` dict passes it
  through.
- **Test hermeticity**: autouse conftest fixture deletes `GEMINI_API_KEY` so no
  test can reach the live endpoint; Gemma-path tests monkeypatch
  `_chat_completion`.
- **Prod**: flip `draft_auto_approve` to 0 for the live user via the guarded
  `ssh prod` python heredoc (PlistBuddy-sourced DATABASE_URL, abort if
  missing); supersede memory note f25bf34b (records ENABLED=1).

## Decisions
- [x] Provider registry + client live in the neutral `llm_client.py`; analytics
      never imports from web; chat_engine re-imports the same names.
      verify: present "PROVIDER_REGISTRY" src/kroger_mcp/llm_client.py
      verify: absent "from ..web" src/kroger_mcp/analytics/draft_selection.py
- [x] Gemma picks from saved recipes only, with season/holiday context; strict
      validation of ids and count; reasons persisted as meal-entry notes.
      verify: present "def select_dinners_with_llm" src/kroger_mcp/analytics/draft_selection.py
- [x] Any LLM failure silently falls back to rotation; the draft is still
      created and `selection_mode` reports which path ran.
      verify: present "selection_mode" src/kroger_mcp/analytics/meal_planning.py
- [x] No test can hit the live Gemma endpoint (autouse GEMINI_API_KEY strip).
      verify: present "GEMINI_API_KEY" tests/conftest.py
- [x] Prod `draft_auto_approve` reset to 0 for the live user; memory note
      f25bf34b superseded.
      manual: plans/gemma-seasonal-draft.md — evidence in session: guarded ssh heredoc output shows draft_auto_approve=0

## Acceptance Criteria
- [x] With a mocked Gemma success, `generate_draft` uses exactly the returned
      recipe ids in order, marks `selection_mode: "gemma"`, and stores reasons
      in `meal_entries.notes`.
      manual: tests/test_gemma_draft.py
- [x] Error dict / bad JSON / invalid ids / raised exception each fall back to
      rotation with a created draft and `selection_mode: "rotation"`.
      manual: tests/test_gemma_draft.py
- [x] Existing chat providers keep working after the extraction.
      verify: tests tests/test_chat_stream.py tests/test_concurrency.py -q
- [x] New and existing draft coverage passes; lint and mypy clean.
      verify: tests tests/test_gemma_draft.py tests/test_weekly_draft.py -q
- [x] Docs updated: CLAUDE.md passive workflow mentions Gemma seasonal picks +
      fallback; tests/README.md covers the new test file.
      verify: present "Gemma" CLAUDE.md
      verify: present "test_gemma_draft" tests/README.md

## Tasks
- [x] Extract provider registry + client into llm_client.py; rewire chat_engine
- [x] Implement draft_selection.py (prompt, parse, select, season helper)
- [x] Integrate LLM selection into generate_draft with notes + selection_mode
- [x] Pass selection_mode through get_attention's weekly_draft dict
- [x] Add GEMINI_API_KEY strip fixture to tests/conftest.py
- [x] Write tests/test_gemma_draft.py; update tests/README.md
- [x] Update project CLAUDE.md passive workflow section
- [x] Run full suite + ruff + mypy
- [x] Flip prod draft_auto_approve to 0 for live user; supersede memory f25bf34b
- [x] Commit and push (deploy) once the mini is reachable

<!-- last-verified: 2026-09-04 -->
