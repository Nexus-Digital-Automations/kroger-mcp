# Spec: Cook → editable pantry deduction, with reminders & undo

## Context
Today, checking the "cooked" box on a meal-plan cell (`meal_plan.html:146` `toggleCooked`)
immediately and **silently** deducts the recipe's full servings-scaled ingredient amounts
from the pantry via `mark_meal_cooked` → `consume_from_pantry`. The user wants this to be
interactive: when you cook something, confirm/edit **how much you actually used**, see
**what's left**, and be able to **undo**. Plus three approved add-ons.

## Scope (all approved)
1. **Editable cook deduction popup** — fires from (a) the meal-plan cooked checkbox (replacing
   the silent deduction) and (b) a new "I made this" button on the recipe page (ad-hoc cook,
   no scheduled meal needed). Each ingredient is prefilled with the scaled recipe amount,
   editable, with current pantry level shown; one-tap "Used as planned" accepts all.
2. **Post-cook feedback** — after deducting, show each ingredient's new remaining level and
   flag anything now low/out.
3. **Add-on A — low-stock → shopping list**: one-tap "Add to list" per flagged low/out item.
4. **Add-on B — reminders**: dashboard card for scheduled meals with `meal_date < today` and
   `cooked_at IS NULL`.
5. **Add-on C — undo**: reverse a cook's pantry deduction exactly (and clear cook flags).

## Key grounded facts
- Pantry "how much is left" = `pantry_items.level_percent` (0–100 int). Status from `low_threshold`
  (default 20): `out` (≤0), `low` (≤threshold), `ok` (>threshold), `untracked` (no pantry row).
- `consume_from_pantry(product_id, quantity, unit, percent=None, source_type, source_id, …)`
  reduces `level_percent` by `deduction` (= `quantity*100/avg_days` capped at 50, or `percent`),
  returns `previous_level`, `new_level`, `amount_deducted`, `remaining_display`. It needs an
  existing pantry row (else `{success:false}`), and writes a `purchase_events` row.
- **Reversal trap**: `purchase_events.quantity_delta` stores `-quantity` (raw amount), NOT the
  percentage-points removed. Undo therefore CANNOT recompute the exact deduction from
  `purchase_events`. → We add a dedicated reversal ledger that records `amount_deducted`.
- DB confirmed multi-tenant: `pantry_items.user_id`, `purchase_events.user_id`, `meal_entries.user_id`,
  `meal_entries.cooked_at`/`pantry_deducted` all exist.
- Reuse: `/api/shopping-list/items` (`AddItemBody{product_id,name,quantity,unit}`) for add-on A;
  recipes API router (`api/recipes.py`) for ad-hoc endpoints; dashboard `_get_pantry_alerts`
  pattern + attention-card layout for add-on B; `recipe_add_modal` store pattern for the popup.

## Design

### Data (database.py)
Add `CREATE TABLE IF NOT EXISTS cook_deductions` (new table → safe on existing DBs via
`initialize_database`): `id PK, cook_event_id TEXT, source_type TEXT, product_id TEXT,
deducted_percent REAL, previous_level INTEGER, user_id TEXT, created_at TEXT`.
This is the exact-amount reversal ledger. `cook_event_id` = meal entry id (meal cooks) or a
uuid4 (ad-hoc).

### Backend (meal_planning.py)
- `_deduct_ingredients(ingredients, *, source_type, cook_event_id, recipe_id, recipe_name,
  source_description, user_id, actuals=None)` — extract the loop currently inline in
  `mark_meal_cooked` (lines ~1330–1392). For each ingredient: pick quantity from `actuals`
  (matched by product_id) else scaled recipe qty; skip if no product_id/qty
  (`skipped_no_product_id`); call `consume_from_pantry`; on success record a `cook_deductions`
  row (`amount_deducted`, `previous_level`) and append a deduction dict augmented with
  `now_low`/`now_out` (compare `new_level` to the item's `low_threshold`). Returns
  `{deductions, deduction_errors, skipped_no_product_id}`.
- `_build_cook_preview_ingredients(ingredients, user_id)` — per ingredient, `get_pantry_item`
  → `current_level_percent`, `current_level_display`, `low_threshold`, `status`. Shared by both previews.
- `preview_meal_cook(plan_id, meal_date, meal_slot, user_id)` and
  `preview_recipe_cook(recipe_id, servings_override, user_id)` — collect+scale ingredients
  (reuse `_collect_ingredients_recursive` + fallback as in `check_meal_pantry_availability`),
  run `_build_cook_preview_ingredients`. Meal variant also returns `already_cooked`/`already_deducted`.
- `mark_meal_cooked(...)` — add params `actuals: list[dict]|None=None`, `deduct: bool=True`;
  delegate deduction to `_deduct_ingredients`; keep `cooked_at`/`pantry_deducted` bookkeeping.
- `undo_meal_cooked(plan_id, meal_date, meal_slot, user_id)` — owner-scoped; idempotent
  (if `cooked_at IS NULL` → `{success, message:"already undone"}`); for each `cook_deductions`
  row (`cook_event_id=entry_id, source_type='meal_plan'`) restore via
  `update_pantry_level(product_id, min(100, current+deducted_percent))`, delete rows; clear
  `cooked_at`/`pantry_deducted`.
- `cook_recipe_adhoc(recipe_id, servings_override=None, actuals=None, deduct=True, user_id)` —
  uuid4 `cook_event_id`; collect ingredients; `_deduct_ingredients(source_type='recipe_adhoc',
  cook_event_id=…)`; NO meal_entries write.
- `undo_recipe_adhoc(cook_event_id, user_id)` — mirror `undo_meal_cooked` minus meal_entries,
  keyed on `source_type='recipe_adhoc'`; idempotent (rows deleted on first undo).

### API
- `api/meal_plan.py`: extend `MarkCookedBody` → `{cooked:bool=True, deduct:bool=True,
  actuals:list[ActualIngredient]|None=None}`; `ActualIngredient{product_id:str, name:str,
  quantity:float, unit:str=""}`. In the cooked handler: pass `actuals`/`deduct` on the
  `cooked:true` branch; **replace** the bare `cooked_at=NULL` else-branch with `undo_meal_cooked`.
  Add `GET …/cook-preview` → `preview_meal_cook`.
- `api/recipes.py`: add `GET /api/recipes/{id}/cook-preview`, `POST /api/recipes/{id}/cooked`
  (→ `cook_recipe_adhoc`, returns `cook_event_id`), `POST /api/recipes/cooked/{cook_event_id}/undo`.
- Add-on A reuses `POST /api/shopping-list/items` (no new endpoint).

### Dashboard (dashboard.py + dashboard.html)
- `_get_uncooked_past_meals(user_id)` (mirror `_get_pantry_alerts`): join `meal_entries`+`meal_plans`,
  `meal_date < today AND cooked_at IS NULL`, owner-scoped, recipe-name via `_load_recipes` with
  fallback to id, compute `days_overdue`. Add `uncooked_past_meals` to context.
- Fourth attention card after "Overdue Favorites" gated on `{% if uncooked_past_meals %}`,
  copying that card's structure; rows link to `/meal-plan`. (Persistent card, NOT a toast.)

### Frontend
- New macro `_macros/cook_modal.html` with `Alpine.store('cookPreview')`, modeled on
  `recipe_add_modal`'s store/markup. `openModal(mode, ids)` GETs the matching cook-preview;
  rows show editable qty stepper (prefilled `scaled_quantity`) + current level + status pill;
  "Used as planned" submits unedited; edits submit `actuals`. On success flips to a results
  view: new remaining levels, low/out flags, one-tap "Add to list" per flagged row
  (`POST /api/shopping-list/items`), and an "Undo" button. Transient confirmation via global
  `toast:show`.
- `meal_plan.html`: render `{{ cook_modal() }}`; change `toggleCooked` so checking opens the
  popup (revert the checkbox until commit; popup performs the PATCH with actuals; reload cell on
  success) and unchecking calls `cooked:false` (→ real undo).
- `recipe_view.html`: render `{{ cook_modal() }}`; add an "I made this" header button →
  `openModal('recipe', {recipeId, servings})`.

## Acceptance criteria
1. `GET …/cook-preview` (meal) and `GET /api/recipes/{id}/cook-preview` return ingredients with
   `name, product_id, scaled_quantity, unit, current_level_percent, current_level_display,
   low_threshold, status∈{ok,low,out,untracked}`; meal variant also `already_cooked`/`already_deducted`.
2. Checking a meal's cooked box NO LONGER deducts silently — it opens the popup prefilled with
   scaled amounts + current levels; nothing deducts until the user confirms.
3. "Used as planned" deducts unedited scaled amounts; editing a qty then confirming deducts the
   edited amounts (verified: `cook_deductions`/`purchase_events` reflect edited qty).
4. After a scheduled cook: `meal_entries.cooked_at` set, `pantry_deducted=1`; each ingredient
   shows new remaining level; items crossing `low_threshold`/≤0 are flagged low/out.
5. Each flagged item offers one-tap "Add to list" → `POST /api/shopping-list/items` with its
   product_id/name/quantity/unit; list count increments.
6. `POST /api/recipes/{id}/cooked` deducts pantry for an unscheduled recipe WITHOUT creating or
   modifying any `meal_entries` row; returns a `cook_event_id`; ledger rows use `source_type='recipe_adhoc'`.
7. Unchecking a cooked meal reverses the deduction **exactly** (levels restored from
   `cook_deductions.deducted_percent`), deletes the ledger rows, clears `cooked_at`/`pantry_deducted`.
8. Undo is idempotent: undoing an already-undone meal returns success with no pantry change;
   ad-hoc undo twice restores once then no-ops.
9. Dashboard shows an "Uncooked Scheduled Meals" card only when such meals exist (recipe, slot,
   date, days-overdue badge), owner-scoped, graceful name fallback for deleted recipes.
10. All new queries/mutations owner-scoped by `user_id`; ingredients without product_id/pantry
    row go to `skipped_no_product_id` and are never deducted.
11. `mark_meal_cooked` and `cook_recipe_adhoc` both deduct via the shared `_deduct_ingredients`.
12. Whole-repo lint/typecheck clean; templates compile.

## Verification
- Python: unit-exercise `_deduct_ingredients`/preview/undo against a throwaway user + product,
  asserting deduct → ledger row → undo restores exactly → idempotent; clean up.
- Live (authenticated browser, throwaway data only): cook-preview shape; checkbox opens popup;
  edit+confirm deducts edited amount; results show new levels + low flag; add-to-list; undo via
  uncheck restores; ad-hoc "I made this" + undo; dashboard reminder card renders.
- Do NOT mutate the user's real pantry during testing.
```
## Tasks
- [ ] Add cook_deductions reversal-ledger table in database.py
- [ ] Extract _deduct_ingredients + add preview builders in meal_planning.py
- [ ] Add actuals/deduct to mark_meal_cooked and undo_meal_cooked
- [ ] Add cook_recipe_adhoc + undo_recipe_adhoc (ad-hoc, no meal_entry)
- [ ] Wire API: extend MarkCookedBody/cooked handler + meal cook-preview
- [ ] Wire API: recipes cook-preview, cooked, undo endpoints
- [ ] Add dashboard uncooked-past-meals reminder card
- [ ] Build cook_modal macro (editable deduct + results + add-to-list + undo)
- [ ] Wire cook_modal into meal_plan checkbox and recipe_view button
- [ ] Verify backend (throwaway data) + live browser end-to-end
```
