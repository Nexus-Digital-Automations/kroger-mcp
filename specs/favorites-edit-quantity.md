# Spec: Edit item quantity on the favorites list page

## Context
The favorites detail page (`favorites_detail.html`) renders each list item in a table
(Item · Brand · Pantry Level · Status · Ordered · Actions). Each item already stores a
`default_quantity` (used to pre-fill Add-to-list / Add-to-cart and the favorite sale-alert
actions), but it is never shown or editable in the UI. Add an inline, editable quantity per
item. The analytics layer already supports the write via
`analytics/favorites.py::update_list_item(default_quantity=...)`; the gap is an API route and
the UI control.

## Decisions
- [x] Auto-save on change (debounced), with a brief "Saved" flash — no explicit Save button.
- [x] The edited field is `default_quantity` (one integer per item, minimum 1), used everywhere
      (add-to-list, add-to-cart, sale alerts).
- [x] Reuse the existing `update_list_item` analytics function; add a user-scoped PATCH route.

## Acceptance Criteria
- [x] A PATCH route `/api/favorites/lists/{list_id}/items/{product_id}` updates the item's
      `default_quantity`, scoped to the logged-in user, returning the saved value.
  verify: tests tests/test_favorites_quantity_api.py
- [x] The favorites detail page shows an editable numeric Qty control on each item row with
      +/− steppers (min 1, integer).
  manual: src/kroger_mcp/web/templates/favorites_detail.html
- [x] Changing the quantity auto-saves (debounced) to `default_quantity` and shows a "Saved"
      confirmation; the new value persists across a page reload.
  manual: src/kroger_mcp/web/templates/favorites_detail.html
- [x] Quantities below 1 or non-integer are rejected/clamped (server returns the corrected
      value; client never persists < 1).
  verify: tests tests/test_favorites_quantity_api.py
- [x] Lint + type check clean for changed Python.
  verify: cmd cd "/Users/jeremyparker/Desktop/Claude Coding Projects/Smart Shopper" && uv run ruff check src/kroger_mcp/web/routes/api/favorites.py
