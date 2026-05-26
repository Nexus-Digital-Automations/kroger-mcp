# Spec — Show QTY / UNIT / INGREDIENT headers in recipe view mode

## Why
Reading mode currently hides the column header strip and renders ingredient rows as a flowing flex-wrap list. The user wants the same labelled table look as edit mode while reading, just without the edit chrome.

## Acceptance criteria
1. **Headers visible in view mode.** The `.ing-columns` strip ("QTY", "UNIT", "INGREDIENT") renders in both `data-recipe-mode="edit"` and `data-recipe-mode="view"`.
2. **Rows align under the headers in view mode.** Each `[data-ingredient-row]` uses the same column grid as the header (`grid-template-columns: 0.5rem 2.25rem 4.5rem 1fr 1.5rem`). Qty sits under "QTY", unit under "UNIT", name under "INGREDIENT" for every ingredient row.
3. **Edit-only chrome stays hidden in view mode.** Drag handle (`⋮⋮`) and remove button (`✕`) — both `edit-only` — remain invisible while reading. Their grid tracks (0.5rem and 1.5rem) are empty but reserved so the columns line up.
4. **Empty unit cell collapses gracefully.** When an ingredient has no unit (e.g. `2 eggs`), the column stays present (alignment intact) but the cell shows nothing — no placeholder text.
5. **Edit mode unchanged.** All edit-mode behaviour (inline-edit on click, drag handle on hover, remove button, autocomplete popover) keeps working exactly as it does today.
6. **Whole-repo clean.** Ruff still green; existing pytest suite still green.

## Files
- `src/kroger_mcp/web/templates/recipe_detail.html` only — CSS block near lines 295–320.

## Verification
1. Playwright at 1800×1000: navigate to `/recipes/6a589938`, default (view) mode. Assert `.ing-columns` is visible. Assert each row's name starts at the same x-coordinate as the "INGREDIENT" header text.
2. Toggle into edit mode. Assert headers still show. Assert drag handle and ✕ appear on hover.
3. Toggle back to view mode. Assert drag handle and ✕ are hidden again.
4. `mcp__lint-digester__run` and `pytest tests/test_price_per_unit.py` — both green.
