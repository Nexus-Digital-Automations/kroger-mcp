# Dashboard: single ranked "Needs your attention" feed

## Context

Today the dashboard shows six independent, equal-weight cards (stat tiles,
Pantry Needs Attention, This Week's Meals, Uncooked Scheduled Meals, Overdue
Favorites, Smart Suggestions). User approved (via sketched artifact
comparing "Current" vs "Proposed") replacing this with one ranked feed of
action items in three urgency tiers — Critical / This week / Plan ahead —
plus a calm "This week at a glance" meal strip and a quiet stat line below
the fold. The bell keeps its own lightweight badge/dropdown unchanged.

Research (context-explorer, this session) found two real bugs to fix while
touching this code:
- `dashboard.py::_get_pantry_alerts` reads raw `pantry_items` columns
  directly instead of `analytics.pantry.get_pantry_status`, so it can
  disagree with the bell (which already uses `get_pantry_status` via
  `list_pantry_alerts_for_bell`) — no depletion recalculation, no `status`
  field, ad-hoc OUT/low logic.
- `dashboard.py::_get_uncooked_past_meals` filters only
  `meal_date < today AND cooked_at IS NULL`, missing the `cook_skipped`/
  `pantry_deducted` filters that `meal_planning.list_pending_meals` (which
  backs the bell's "Meals to confirm") already applies — so a meal the user
  dismissed via the bell's "Didn't cook" button keeps reappearing on the
  dashboard forever.

Both are fixed by routing the dashboard through the same source-of-truth
functions the bell already uses, rather than duplicating query logic.

## Decisions

- [x] Tiers are **Critical** (already late/out — blocks something),
      **This week** (time-sensitive, not urgent yet), **Plan ahead**
      (foresight, no deadline) — matching the approved sketch.
      verify: present "tier-critical" src/kroger_mcp/web/templates/dashboard.html
      verify: present "tier-week" src/kroger_mcp/web/templates/dashboard.html
      verify: present "tier-ahead" src/kroger_mcp/web/templates/dashboard.html
- [x] Pantry alerts move to `pantry.get_pantry_status()` (replacing the raw
      `_PANTRY_ALERT_PREDICATE` SQL) so pantry data agrees between the bell
      and the dashboard everywhere. `status == 'out'` → Critical;
      `status == 'low'` or `days_to_expiration <= 7` → This week.
      verify: absent "_PANTRY_ALERT_PREDICATE" src/kroger_mcp/web/routes/dashboard.py
- [x] "Uncooked meals" and "meals to confirm" are the same underlying data
      (`meal_planning.list_pending_meals`, already correctly filtered) —
      not two separate queries. Tiered by `days_overdue`: >= 3 days →
      Critical, 0-2 days → This week.
      verify: absent "_get_uncooked_past_meals" src/kroger_mcp/web/routes/dashboard.py
- [x] Overdue favorites (all — `is_overdue` already means already-late) →
      Critical, with a real "Reorder" action wired to the existing
      `POST /api/favorites/lists/{list_id}/add-to-shopping-list` endpoint
      instead of just a link.
- [x] Smart suggestions split by their existing `tag_kind`: overdue-repurchase
      (`danger`) → This week; seasonal (`info`) → Plan ahead.
- [x] Favorite-on-sale alerts (`notifications.list_alerts`) → This week,
      reusing the bell's exact List/Cart/View actions (same generic
      endpoints: `POST /api/shopping-list/items`,
      `POST /api/products/{id}/add-to-cart`).
- [x] "Next week needs a plan" (`notifications.next_week_needs_plan`) →
      Plan ahead, single item, links to `/meal-plan`.
- [x] Tiering + the four notifications-sourced lists (alerts, pending meals,
      pantry alerts, needs-plan) are computed **client-side** in a new
      Alpine component (`priorityFeed`, new file
      `static/js/priority_feed.js`) that fetches the existing
      `GET /api/notifications` endpoint — the exact same data the bell
      already polls — rather than re-deriving tiers server-side in Python.
      This guarantees zero drift between the bell's badge and the
      dashboard's feed, and avoids two independent implementations of "is
      this pantry item alert-worthy". `overdue_favorites` and
      `smart_suggestions` (dashboard-only, not part of the bell) are
      server-rendered via `tojson` into a `<script type="application/json">`
      seed tag the component reads in `init()`.
      verify: present "priorityFeed" src/kroger_mcp/web/static/js/priority_feed.js
      verify: present "/api/notifications" src/kroger_mcp/web/static/js/priority_feed.js
- [x] Stat tiles shrink from a 4-tile grid to a quiet inline stat line
      (recipes / meal plans / favorites lists) below the calm meal strip —
      pantry-alert-count is dropped as a standalone number since pantry
      items now surface individually in the ranked feed.

## Acceptance Criteria

- [x] `GET /dashboard` renders one "Needs your attention" feed grouped into
      Critical / This week / Plan ahead, populated from live data (no
      placeholder `href="#"` links).
      verify: absent "_PANTRY_ALERT_PREDICATE" src/kroger_mcp/web/routes/dashboard.py
- [x] Sale-alert rows support List / Cart / View actions identical in
      behavior to the bell (same endpoints, same busy-state handling).
      verify: present "addToCart" src/kroger_mcp/web/static/js/priority_feed.js
      verify: present "/api/shopping-list/items" src/kroger_mcp/web/static/js/priority_feed.js
- [x] Meal rows open the existing global `cookPreview` modal (confirm) or
      POST the existing skip endpoint (didn't cook) — no new meal-plan
      backend logic.
      verify: present "cookPreview" src/kroger_mcp/web/static/js/priority_feed.js
      verify: present "/skip" src/kroger_mcp/web/static/js/priority_feed.js
- [x] Overdue-favorites rows have a working "Reorder" button that calls the
      existing bulk-add endpoint and removes itself from the feed on
      success.
      verify: present "add-to-shopping-list" src/kroger_mcp/web/static/js/priority_feed.js
- [x] Pantry / plan-ahead / smart-suggestion rows link to the correct real
      page (`/pantry`, `/meal-plan`, `/products`).
      verify: present "href=\"/pantry\"" src/kroger_mcp/web/templates/dashboard.html
      verify: present "href=\"/meal-plan\"" src/kroger_mcp/web/templates/dashboard.html
- [x] "This week at a glance" meal strip and the quiet stat line still
      render correctly (server-rendered, unchanged data sources).
      verify: present "week_days" src/kroger_mcp/web/templates/dashboard.html
- [x] Existing dashboard tests (if any) and the two `test_notifications_bell`
      regression tests still pass; whole-repo lint/type-check clean.
      verify: tests tests/test_notifications_bell.py
- [x] Manual smoke test: dev server up, `/dashboard` loads, feed populates
      from `/api/notifications`, at least one action (e.g. List) verified
      to actually call its endpoint via browser network tab or server log.
      manual: output/dashboard-priority-feed-smoke-test.md

## Tasks

- [x] Trim `dashboard.py`: delete `_get_pantry_alerts` /
      `_PANTRY_ALERT_PREDICATE` / `_get_uncooked_past_meals`; keep
      `_get_this_week_meals`, `_get_meal_plan_count`, `_get_overdue_favorites`,
      `_get_smart_suggestions`; update `_dashboard_payload` to drop the
      removed keys and stop passing `pantry_alert_count`.
- [x] Write `static/js/priority_feed.js`: `Alpine.data('priorityFeed', () => ({...}))` —
      fetches `/api/notifications` on init, reads `overdueFavorites`/
      `smartSuggestions` from a server-rendered `<script type="application/json"
      id="priority-feed-seed">` tag (not Alpine.data() args — Jinja's `tojson`
      output isn't attribute-safe), exposes `critical`/`thisWeek`/`planAhead`
      getters, and action methods (`addToList`, `addToCart`, `view`,
      `confirmMeal`, `skipMeal`, `reorderFavorite`) adapted from
      `notifBell` in `notifications.js` (reuse `window.api`, `window._ssToast`,
      `Alpine.store('qtyPicker')`, `Alpine.store('cookPreview')`).
- [x] Rewrite `dashboard.html` body: replace the stat grid + six cards with
      the ranked feed markup (three tier sections, severity-colored left
      border per row per the approved sketch's visual language) + the calm
      "This week at a glance" strip + quiet stat line. Load
      `priority_feed.js` and initialize `x-data="priorityFeed(...)"` with
      server-rendered `overdue_favorites`/`smart_suggestions` via `tojson`.
- [x] Run whole-repo lint + type-check + full test suite; fix any breakage
      from the removed dashboard.py helpers (check for other
      importers/tests referencing them first).
- [x] Manual smoke test via dev server (start it, hit `/dashboard`, confirm
      feed renders and at least one action fires against its real
      endpoint).
- [x] Commit.

<!-- last-verified: 2026-07-19 -->
