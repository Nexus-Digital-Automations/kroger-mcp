---
title: Shopping List Simplification
status: completed
created: 2026-04-18
---

## Vision

Simplify the shopping list page to its core purpose: view and manage the list. Remove all Kroger cart-related UI. Replace the action menu per row with a direct trash icon. Add a clear-all button.

## Requirements

### R1. Remove Kroger cart section and everything below the shopping list
- Remove the entire Kroger Cart section (cart summary bar, search/filter, cart items table, order history)
- Remove the Cart Preview Modal
- Remove the "Send to Cart" button from the shopping list header
- Clean up all now-dead JS: `previewCart()`, `sendToCart()`, `previewItems`, `previewSkipped`, `showPreview`
- Remove `@action-menu:favorites-add.window` and `@action-menu:shopping-remove.window` Alpine event listeners
- Remove the `action_menu` macro import (no longer used on this page)

### R2. Trash icon per row
- Replace `{{ action_menu('shopping_row') }}` in the table's last column with a trash icon button
- Clicking it immediately calls `removeItem(item.id, item.product_id)` — no confirmation
- Icon: standard trash SVG, styled like existing danger actions (`color: var(--danger)`)
- Match the existing row action button style

### R3. Clear Shopping List button
- Add a "Clear List" button in the shopping list card header (next to the item count)
- Only visible when `items.length > 0`
- On click: show a browser `confirm()` dialog — "Clear all items from your shopping list?"
- If confirmed: call `DELETE /api/shopping-list` to wipe the list, then reload
- If the endpoint doesn't exist: add it to the shopping-list route handler
- Styled as `ss-btn-secondary` (not primary — it's a destructive action)

## Acceptance Criteria

- [x] The Kroger Cart section is gone — no cart summary, no cart items table, no Order History, no filter panel
- [x] The Cart Preview Modal is gone
- [x] "Send to Cart" button is removed from the shopping list header
- [x] Each row has a trash icon button that immediately removes the item
- [x] "Clear List" button appears in the header when items exist
- [x] Clicking "Clear List" shows a confirm dialog; cancelling does nothing
- [x] Confirming "Clear List" removes all items and refreshes the table
- [x] `DELETE /api/shopping-list` endpoint exists and clears all items
- [x] No dead JS remains (previewCart, sendToCart, previewItems, previewSkipped, showPreview, cart Alpine event listeners)
- [x] `action_menu` macro is no longer imported on this page

## Technical Decisions

- Use browser `confirm()` for the clear dialog — no custom modal needed, keeps it simple
- `DELETE /api/shopping-list` (no item ID) = clear-all; `DELETE /api/shopping-list/{id}` = single item (existing)
- Trash icon uses inline SVG consistent with the rest of the app (no icon lib dependency)

## Progress

- [ ] Spec approved
- [ ] Backend: add `DELETE /api/shopping-list` clear-all endpoint
- [ ] Frontend: rewrite shopping_list.html
