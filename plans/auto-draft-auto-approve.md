# Server-side auto-draft + opt-in auto-approve

## Context

Follow-up to the passive weekly workflow (plans/i-am-a-lazy-memoized-fog.md, complete).
User approved both remaining passivity levers:
1. **Server-side auto-draft** — `pantry(action='get_attention')` creates next week's
   draft itself when one is due, instead of only flagging it. The draft is always
   ready and waiting; no dependency on the assistant following CLAUDE.md.
2. **Auto-approve (opt-in)** — a `draft_auto_approve` setting (default OFF). When on,
   the generated plan is created live (not a draft): zero weekly touchpoints;
   corrections happen after the fact via `skip_meal`.

No schema changes: the setting uses the existing `user_settings` key/value accessor
pattern in `tools/shared.py`, so there is nothing to mirror to Postgres.

## Design

- `get/set_draft_auto_approve` in `tools/shared.py` — int-backed bool (0/1),
  default 0, mirroring the other three planning settings. Validate value ∈ {0, 1}.
- `generate_draft` resolves the setting; when on, passes `is_draft=False` to
  `create_meal_plan` (created live directly — no create-then-flip window) and
  returns `is_draft: False, auto_approved: True`. Idempotency holds: a live plan
  covering next week hits the existing `already_planned` branch on repeat calls.
  An existing unapproved draft is NOT retroactively approved by flipping the
  setting on — it still needs one explicit `approve_draft`.
- `get_attention` (`tools/prediction_tools.py`): after the reconcile
  fire-and-discard block, in its own try/except: if `next_week_needs_plan(user_id)`
  → `generate_draft(user_id=user_id)`; include the result under a `weekly_draft`
  response key only when `result.get("success")`. Failures (e.g. zero saved
  recipes) stay silent here — the bell keeps flagging `needs_plan`, so nothing is
  lost. Never let this block break `get_attention`.
- Bell (`analytics/notifications.py` + `web/routes/api/notifications.py`):
  distinguish "draft awaiting approval" from "no plan yet" — new helper
  `draft_awaiting_approval(user_id)` returning the pending draft's id/name for
  next week's start (or None), surfaced in the notifications payload so the web
  bell can say "approve the draft" instead of "no plan".
- Expose the setting via `info` tool (`set_draft_auto_approve`, `value` 0/1;
  `get_preferences` gains `draft_auto_approve`) and web settings API
  (GET field + POST `/api/settings/draft-auto-approve`).
- `scripts/smoke_mcp_spec.py` gains the new info action entry.
- Docs: update project CLAUDE.md Passive Weekly Workflow section — the draft now
  appears on its own; with auto-approve on there is no weekly touchpoint at all.
- Enable the setting for the user's account (they chose it): via live server if
  reachable, else document the one-call enable for the next shopping session.

## Decisions
- [x] `draft_auto_approve` is an int-backed bool setting, default 0 (off),
      accessor pair in shared.py; no schema change on either backend.
      verify: present "def get_draft_auto_approve" src/kroger_mcp/tools/shared.py
- [x] `generate_draft` honors it by creating the plan live (`is_draft=False`) and
      reporting `auto_approved: True`; existing drafts are not retro-approved.
      verify: present "auto_approved" src/kroger_mcp/analytics/meal_planning.py
- [x] `get_attention` auto-generates the draft when a week is due; surfaced under
      `weekly_draft` only on success; failures never break the call.
      verify: present "weekly_draft" src/kroger_mcp/tools/prediction_tools.py
- [x] Bell distinguishes draft-awaiting-approval from needs-plan.
      verify: present "def draft_awaiting_approval" src/kroger_mcp/analytics/notifications.py

## Acceptance Criteria
- [x] With auto-approve on, `generate_draft` returns a live plan whose past-dated
      meals reconcile like any other plan.
      manual: tests/test_weekly_draft.py
- [x] Setting reachable via `info` tool action and web settings routes.
      verify: present "set_draft_auto_approve" src/kroger_mcp/tools/info_tools.py
      verify: present "draft-auto-approve" src/kroger_mcp/web/routes/api/settings.py
- [x] Smoke spec covers the new action.
      verify: present "set_draft_auto_approve" scripts/smoke_mcp_spec.py
- [x] New and existing coverage passes.
      verify: tests tests/test_weekly_draft.py tests/test_snack_log.py tests/test_notifications_bell.py -q

## Tasks
- [x] Add get/set_draft_auto_approve accessor pair to shared.py
- [x] Honor the setting in generate_draft (live plan + auto_approved flag)
- [x] Auto-generate draft in get_attention; surface weekly_draft key
- [x] Add draft_awaiting_approval helper; wire into web bell payload
- [x] Expose setting via info tool, web settings routes, smoke spec
- [x] Tests for auto-approve path, get_attention surfacing, bell helper
- [x] Update CLAUDE.md passive workflow section
- [ ] Enable the setting for the live user account (or document one-call enable)
