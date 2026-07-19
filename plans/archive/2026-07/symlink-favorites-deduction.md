# Symlink management popup, favorites-driven pantry drain, default meal-plan deduction

## Context
Three related improvements, grilled and locked via AskUserQuestion:

1. Recipe ingredient "Kroger-linked" dot currently only supports unlink (confirm() + clear
   product_id). Replace with the existing rich ingredient-linker popover so users can search
   and relink, with an explicit Unlink action inside the popover.
2. Pantry items should drain according to the cadence of the favorites list(s) they belong to
   (matched by product_id), not just a manually-set daily_depletion_rate.
3. Meal-plan pantry deduction should default to 'automatic' (currently 'confirm'), with a
   one-time toast informing affected users they can change it back in Settings.

## Decisions
- [x] Symlink popup: reuse `ingredient_linker_popover.html` / `linker_popover.js` (the existing
      search/usuals/detail popover), wired to the green "Kroger-linked" dot's click instead of
      building a new modal. Add an explicit "Unlink" action inside the popover itself.
- [x] Pantry drain model: smooth continuous depletion (existing `daily_depletion_rate` glide
      model), auto-derived from favorites cadence rather than a hard reset-to-zero.
- [x] Cadence source: `favorite_list_items.typical_gap_days` first; fall back to the parent
      `favorite_lists.reorder_weeks` (×7 days) when the item has no learned gap. If a product
      matches multiple lists/cadences, use the shortest (most frequent, i.e. highest rate).
- [x] Favorite linkage implies auto-depletion: a pantry item matching a cadenced favorite is
      treated as auto-depleting (using the favorite-derived rate) even if its own `auto_deplete`
      flag/manual rate says otherwise. Items with no favorites match are unaffected — manual
      config keeps working exactly as before.
- [x] Meal-plan deduction default flips from 'confirm' to 'automatic' for any user with no
      explicit saved preference. Users who already explicitly chose 'confirm' or 'automatic'
      are unaffected (their stored choice always wins over the default).
- [x] Notification: one-time dismissible toast via the existing `_ssToast` system, shown only to
      users who are relying on the (now-flipped) default, tracked server-side so it never
      reappears once dismissed.

## Acceptance Criteria
- [x] Clicking the green Kroger-linked dot on a recipe ingredient (in edit mode) opens the same
      search popover used for manual/unlinked ingredients, pre-seeded with the current
      ingredient name, allowing the user to pick a different Kroger product.
      verify: present "acOpenFor(ing._idx, ing.name" src/kroger_mcp/web/templates/recipe_edit.html
      manual: confirmed live in browser against the local dev server — clicking the dot opens
      the popover pre-seeded with the ingredient name and Kroger search results.
- [x] The popover shows an "Unlink" affordance when open for an already-linked ingredient, and
      using it clears the product link the same way the old confirm-dialog did.
      verify: present "acUnlinkCurrent" src/kroger_mcp/web/static/js/linker_popover.js
      manual: confirmed live — "Linked: <name> · Unlink" bar renders for a linked ingredient;
      clicking Unlink clears product_id (toast "Saved unlink", dot reverts to unlinked).
- [x] `get_pantry_status` derives an effective depletion rate from matching favorite list
      cadence (item-level `typical_gap_days` first, list-level `reorder_weeks*7` fallback,
      shortest cadence wins across multiple lists) and applies it even when the pantry item's
      own `auto_deplete` is off.
      verify: present "get_favorite_depletion_rates" src/kroger_mcp/analytics/pantry.py
      manual: confirmed live — a pantry item linked to a `reorder_weeks=1` favorites list showed
      "Days Left: 7.0d" at 100% (100 / (100/7) = 7), matching the derived weekly cadence.
- [x] `get_meal_plan_pantry_deduction_mode` defaults to 'automatic' when no preference is saved.
      verify: present "\"meal_plan_pantry_deduction_mode\", \"automatic\"" src/kroger_mcp/tools/shared.py
- [x] A user relying on the default (no explicit saved mode) sees a one-time toast on next page
      load explaining the automatic-deduction default and linking to Settings; dismissing it
      persists so it never shows again for that user.
      verify: present "mark_deduction_default_notice_seen" src/kroger_mcp/tools/shared.py
      manual: confirmed live — toast appeared verbatim on first dashboard load for a freshly
      registered account and did not reappear after dismissal.

## Tasks
- [x] Wire the Kroger-linked dot's click handler to `acOpenFor` in recipe_edit.html and
      recipe_view.html; update its title/aria-label copy.
- [x] Add `acCurrentIng` + `acUnlinkCurrent()` methods to linker_popover.js; add the
      "currently linked / Unlink" bar to ingredient_linker_popover.html with matching CSS in
      both recipe templates. (Both `acCurrentIng` and the pre-existing `acPopoverStyle` had to be
      plain methods, not getters — this mixin is merged into the Alpine component via object
      spread, which reads/freezes accessor properties at spread time instead of preserving them
      as live getters. Found and fixed as part of manual verification; `acPopoverStyle` had been
      silently broken — frozen at `left:0px;top:0px;` — since before this session.)
- [x] Add `get_favorite_depletion_rates(user_id)` to favorites.py (batched, one query). (Landed in
      a new `favorite_depletion.py` module instead, to stay clear of the pantry/favorites
      file-size gate.)
- [x] Use it inside `get_pantry_status` in pantry.py to override the effective rate/auto_deplete
      per item. (Dropped the `depletion_source` transparency field to stay line-neutral against
      the file-size gate — not required by the Acceptance Criteria.)
- [x] Flip the default in `get_meal_plan_pantry_deduction_mode` (shared.py) to 'automatic'.
- [x] Add `should_show_deduction_default_notice` / `mark_deduction_default_notice_seen` helpers
      to shared.py, thread through `GET /api/settings` and a new dismiss route.
- [x] Show the toast client-side (base template or settings bootstrap JS) and call the dismiss
      route on close.
- [x] Manually verify: recipe ingredient relink flow, pantry status reflecting a weekly favorite
      item's derived rate, and the one-time toast appearing/disappearing correctly. Also
      regenerated `linker_popover.min.js` via `scripts/build_js.sh` and bumped its `?v=` cache-bust
      in both recipe templates — the committed minified bundle was stale relative to source and
      is what the browser actually serves.
