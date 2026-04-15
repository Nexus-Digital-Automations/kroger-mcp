---
title: Remove cart view from shopping list page
status: completed
created: 2026-04-12
---

## Vision

Remove the redundant local cart display from the shopping list page. Users manage their cart in the Kroger app. The "Send to Cart" button must continue to send items to the real Kroger cart via the API.

## User Request

> "please get rid of the cart view on the shopping list page and make sure the send to cart actually sends the items to the kroger cart"

Received: 2026-04-12T20:15 CDT

## Requirements

1. Remove the Kroger Cart section (summary bar, filters, items table, order history) from the shopping list page template.
2. Remove the `cartData()` Alpine.js function and all cart-related JavaScript from the template.
3. Remove unused cart data imports and template variables from the page route.
4. Keep the "Send to Cart" button and preview modal intact.
5. Ensure the send-to-cart flow continues to call `client.cart.add_to_cart()` (Kroger API).
6. Add a status message banner so users see confirmation after items are sent.

## Acceptance Criteria

- [x] Cart view section (summary, filters, table, order history) is removed from shopping list template
- [x] `cartData()` JS function is removed from the template
- [x] `_load_cart_data` and `_load_order_history` imports removed from page route
- [x] `current_cart` and `order_history` template variables removed from page route
- [x] "Send to Cart" button still visible when items exist
- [x] Preview modal still opens on "Send to Cart" click
- [x] `sendToCart()` still POSTs to `/api/shopping-list/add-to-cart` with `confirm: true`
- [x] Backend endpoint still calls `client.cart.add_to_cart()` (Kroger API) on confirm
- [x] Status message banner displays after cart send
- [x] `/shopping-list` page loads without JS errors
- [x] All Playwright tests pass (test_all_features: 57/57, test_all_buttons: 121/121)
- [x] pytest passes, ruff lint passes, security scan clean

## Technical Decisions

- Removed ~222 lines of cart display code (HTML + JS) from the template
- Kept the `/api/shopping-list/add-to-cart` endpoint unchanged — it already calls the real Kroger API
- Added `x-show="msg"` status banner to the `shoppingListData` Alpine scope for user feedback

## Progress

- 2026-04-12: Implemented all changes, committed as f6cd2d4, pushed to main
- 2026-04-12: Playwright verification — 57/57 features, 121/121 buttons pass
