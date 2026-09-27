# Shopping list: "Mark Empty" button

## Context
Quick reset: set the pantry level of every shopping-list item to 0% in one click.

## Decisions
- [x] Button label is "Mark Empty", beside "Clear List"
  verify: present "Mark Empty" src/kroger_mcp/web/templates/shopping_list.html
- [x] List items not tracked in the pantry (and manual items with no product_id) are skipped, not added
  verify: tests tests/test_shopping_list_mark_empty.py
- [x] Goes through update_pantry_level, so depletion events are recorded like a manual 0% set
  verify: present "update_pantry_level" src/kroger_mcp/web/routes/api/shopping_list.py
- [x] Acts on all list items, ignoring the filter box
  verify: absent "filteredItems.*mark" src/kroger_mcp/web/templates/shopping_list.html

## Acceptance Criteria
- [x] POST /api/shopping-list/mark-empty zeroes tracked items and reports emptied/skipped counts; shopping list itself is unchanged
  verify: tests tests/test_shopping_list_mark_empty.py
