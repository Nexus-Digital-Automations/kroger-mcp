---
title: Snacks section + pre-cart replenishment check-up
status: completed
created: 2026-06-22
---

## Approval Trail
- 2026-06-22 — user requested: "add a 'snacks' section and list of favorites
  that get eaten without a schedule, but should be checked up on before the
  list is sent to cart about which 'snacks' likely need to be replenished."
- Claude grilled via two AskUserQuestion rounds. Locked answers below.

## Vision
Snacks are favorites eaten on no fixed schedule, so the existing per-list
`reorder_weeks` cadence doesn't fit them. Model snacks as a dedicated
favorites list (`list_type='snacks'`, no reorder schedule). Before the
shopping list is sent to the Kroger cart, surface a **snack check-up**: a
checklist of snacks that *likely* need replenishing — pre-ticked by a
depletion-and-staleness heuristic — so the user confirms which to add to the
list before it goes to cart. Ships on both the web UI and the Kroger MCP tool.

## Decisions
- [x] Snacks are a dedicated favorites list with `list_type='snacks'` and no
  reorder schedule (`reorder_weeks` stays NULL). A built-in "Snacks" list is
  auto-provisioned per user alongside "My Favorites".
  verify: present src/kroger_mcp/analytics/favorites.py _ensure_snacks_list_for_user
- [x] "Likely needs replenishment" (pre-tick) = pantry level < 30% (when
  tracked) OR days-since-last-ordered ≥ the item's typical gap OR never
  ordered. The user always confirms; nothing is auto-added silently.
  verify: tests tests/test_snacks_checkup.py
- [x] Staleness source: a new per-item `last_ordered_at`, stamped when a snack
  is actually sent to cart, backfilled once from pantry `last_restocked_at`.
  verify: present src/kroger_mcp/analytics/favorites.py mark_snacks_ordered
- [x] Typical gap is a per-item `typical_gap_days` field defaulting to 21.
  verify: cmd python3 -c "import sqlite3;c=sqlite3.connect('data/kroger_analytics.db');assert 'typical_gap_days' in [r[1] for r in c.execute('PRAGMA table_info(favorite_list_items)')]"
- [x] The check-up runs as a step inside the existing "Send to Kroger Cart"
  flow, before the add/skip/manual preview.
  verify: present src/kroger_mcp/web/templates/_macros/cart_send_modal.html inSnackCheck

## Acceptance Criteria
- [x] DB: `favorite_list_items` gains `last_ordered_at TEXT` and
  `typical_gap_days INTEGER` columns (idempotent migration + CREATE TABLE),
  and `last_ordered_at` is backfilled once from `pantry_items.last_restocked_at`.
  verify: cmd python3 -c "import sqlite3;c=sqlite3.connect('data/kroger_analytics.db');cols=[r[1] for r in c.execute('PRAGMA table_info(favorite_list_items)')];assert 'last_ordered_at' in cols and 'typical_gap_days' in cols, cols"
- [x] `favorites.check_snacks(user_id)` returns each snack with `pantry_level`,
  `days_since_ordered`, `typical_gap_days`, `never_ordered`, a `pre_ticked`
  boolean per the heuristic, and a human `reason`. Heuristic covered by tests.
  verify: tests tests/test_snacks_checkup.py
- [x] A built-in "Snacks" list (`list_type='snacks'`) is auto-created per user
  and appears in `get_lists`.
  verify: tests tests/test_snacks_checkup.py
- [x] MCP `favorites(action='check_snacks')` returns the candidate checklist;
  `add_item`/`set_stock_level` accept `typical_gap_days`.
  verify: present src/kroger_mcp/tools/favorites_tools.py check_snacks
- [x] Web: `GET /api/favorites/snacks/check` returns the candidates;
  `POST /api/favorites/snacks/add-to-list` appends chosen snacks to the
  shopping list.
  verify: present src/kroger_mcp/web/routes/api/favorites.py snacks/check
- [x] When snacks need replenishing, the "Send to Kroger Cart" modal shows a
  snack check-up step (pre-ticked candidates) before the existing preview;
  "Continue" adds the ticked snacks to the list, then the preview renders them.
  With zero snack candidates the modal behaves exactly as before (no
  regression).
  verify: present src/kroger_mcp/web/templates/_macros/cart_send_modal.html continueFromSnacks
- [x] Snacks actually sent to cart get `last_ordered_at` stamped (web confirm
  path calls `mark_snacks_ordered`; MCP `order` stamps via
  `increment_times_ordered`).
  verify: present src/kroger_mcp/analytics/favorites.py last_ordered_at
- [x] `ruff check src/` exits 0.
  verify: cmd ruff check src/
</content>
