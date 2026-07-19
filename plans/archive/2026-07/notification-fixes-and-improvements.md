# Notification bell: fix DB split-brain + surface more signals

## Context

User reported "I don't see many notifications, I don't think they're working."
Investigation (context-explorer agent, verified directly against the
production mini) found the sale-alert bell is empty not because of a client
bug, but because the daily background scanner
(`scripts/background_scanner.py`, launchd `com.user.kroger-discount-scanner`)
writes to a local SQLite DB while the live web app reads Postgres
(`DATABASE_URL` is set for `com.smartshopper.web` but not for the scanner's
env). The scanner has correctly found 23 unseen sale alerts over the last 11
days — invisible to the bell purely because of the DB split.

Secondary bug, same script: `scan_watchlist_for_deals()` reads
`KROGER_PREFERRED_LOCATION`, but the production `.env` only defines
`KROGER_LOCATION_ID` (the name every other call site in the app uses —
`tools/shared.py:get_preferred_location_id`). The deal-watchlist half of the
scan has been failing every run as a result.

Beyond the bugs, three signals the backend already computes are never pushed
to the user: pantry low-stock/expiring items (dashboard-only today), a
missing-next-week meal plan, and repurchase/seasonal predictions (chat-tool
only today).

## Decisions

- [x] Fix the split-brain by adding `DATABASE_URL` to the scanner's
      production env (mini `~/kroger-mcp/.env`), not by moving the scan into
      the web app process — smaller blast radius, keeps the two processes
      independent as originally designed.
      verify: manual — SSH check that `~/kroger-mcp/.env` on the mini has a
      `DATABASE_URL` line after the change (value never echoed locally).
- [x] Fix the location env var by renaming the read in
      `background_scanner.py` to `KROGER_LOCATION_ID` (the app-wide
      convention), not by adding a second env var — one canonical name only.
      verify: absent KROGER_PREFERRED_LOCATION scripts/background_scanner.py
- [x] Migrate the 23 orphaned SQLite `favorite_sale_alerts` rows into
      Postgres as part of this fix (not silently dropped) — they're real,
      still-valid evidence of the bug and the whole point of the fix is that
      the user should be able to see them.
      manual: one-off migration run on the mini, row count logged before/after.
- [x] Pantry low-stock/expiring items surface through the existing
      `/api/notifications` payload (new `pantry_alerts` key) and render in
      the bell, reusing `analytics.pantry.get_pantry_status` rather than
      duplicating the dashboard's raw-SQL predicate.
      verify: tests tests/test_notifications_bell.py -k pantry_alerts
- [x] Weekly meal-plan reminder fires when no `meal_plans` row covers next
      Monday, reusing `meal_planning.find_plan_covering_date` — no new
      dismissal state; it naturally clears once a plan is created.
      verify: tests tests/test_notifications_bell.py -k meal_plan_reminder
- [x] Repurchase/seasonal predictions surface as a new "Smart Suggestions"
      dashboard card (not the bell) — they're a browsable list, not a
      discrete alert, so they fit the existing dashboard-card pattern
      (`Pantry Needs Attention`, `Favorites Overdue`) better than the bell.

## Acceptance Criteria

- [x] `GET /api/notifications` includes `pantry_alerts` (list) alongside the
      existing `alerts`/`pending_meals`, and `unseen` counts pantry alerts
      too (consistent with how `pending_meals` already contributes).
      verify: tests tests/test_notifications_bell.py
- [x] The bell dropdown renders a "Pantry needs attention" section when
      `pantry_alerts` is non-empty, matching the existing row style.
      verify: present "Pantry needs attention" src/kroger_mcp/web/templates/_notifications.html
- [x] The bell dropdown renders a "Plan next week's meals" reminder row when
      no plan covers next Monday, linking to `/meal-plan`.
      verify: present "Plan next week" src/kroger_mcp/web/templates/_notifications.html
- [x] `scan_watchlist_for_deals()` no longer errors on `KROGER_LOCATION_ID`
      being unset in an environment where it *is* set (the mismatch is
      gone); regression test locks in the correct env var name.
      verify: tests tests/test_background_scanner_env.py
- [x] Dashboard shows a new "Smart Suggestions" card combining
      `predictions.get_overdue_items` and `seasonal.get_upcoming_seasonal_items`
      via `predictions.get_shopping_suggestions`, hidden when both are empty.
      verify: present "Smart Suggestions" src/kroger_mcp/web/templates/dashboard.html
- [x] Production: scanner env has `DATABASE_URL`; a manually triggered scan
      run writes to the same Postgres `favorite_sale_alerts` table the web
      app reads, and the previously-orphaned 23 SQLite rows appear in
      Postgres (migrated, not lost).
      manual: SSH verification — row counts before/after in both backends.

## Tasks

- [x] Rename `KROGER_PREFERRED_LOCATION` → `KROGER_LOCATION_ID` in
      `scripts/background_scanner.py`; add a small regression test.
- [x] Add `analytics/notifications.list_pantry_alerts_for_bell(user_id)`
      wrapping `get_pantry_status`; wire into `/api/notifications`.
- [x] Add `analytics/notifications.next_week_needs_plan(user_id)` using
      `find_plan_covering_date`; wire into `/api/notifications`.
- [x] Update `notifications.js` (`refresh()`) + `_notifications.html` to
      render the two new sections.
- [x] Add a "Smart Suggestions" card to `dashboard.py`/`dashboard.html`
      sourced from `get_shopping_suggestions()`.
- [x] Write regression tests for the two new bell sections and the env var
      fix.
- [x] Deploy (git push → auto-deploy hook) so the code fix reaches the mini.
- [x] SSH to the mini: add `DATABASE_URL` to the scanner's `.env` (copied
      from the web app's own plist value, never printed locally), migrate
      the 23 SQLite alert rows into Postgres, manually trigger one scan run,
      verify Postgres row counts increased and SQLite is now considered
      historical/orphaned.
- [x] Report final state back to the user.
