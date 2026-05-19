# Feature Ideas — Smart Shopper Frontend Audit
**Date:** 2026-05-03
**Source:** [`specs/frontend-audit-pass.md`](../specs/frontend-audit-pass.md)
**Method:** Walked every in-scope page during audit pass, noting gaps
between what exists and what a Kroger MCP user would wish existed.

---

## Meal Planning

### 1. Generate shopping list from meal plan
**Who:** Weekly meal planners | **Effort:** M | **Priority:** P0

One click turns the current week's meal plan into a fully populated shopping list, deduplicating ingredients and respecting pantry levels. Currently users have to add each recipe's ingredients to the list individually from the recipe detail page.

**UI sketch:** "Generate List" button in the meal plan page header. On click, opens a summary modal ("18 items will be added, 4 are already in your pantry") with Confirm/Cancel. After confirm, navigates to `/shopping-list`.

### 2. Recurring meal schedules
**Who:** Routinized cooks | **Effort:** S | **Priority:** P1

Set meal themes that auto-populate the plan each week: "Taco Tuesday", "Meatless Monday", "Sunday Batch Cook". The current planner is fully manual — every week starts blank.

**UI sketch:** Per-day dropdown "Repeat weekly" toggle in the meal plan grid. A "Manage Recurring" settings panel shows a table of day + label pairs.

### 3. Grocery budget per meal plan week
**Who:** Budget-conscious households | **Effort:** L | **Priority:** P1

When building a meal plan, show the running total cost based on recipe ingredient prices. Currently there's no cost visibility until you open the shopping list.

**UI sketch:** Small "Est. total" badge in the meal plan page header, updated as recipes are assigned. Expandable breakdown: "This week's plan: ~$47.30". Links to weekly analytics.

---

## Smart Shopping

### 4. Aisle-sorted shopping list
**Who:** In-store shoppers | **Effort:** M | **Priority:** P0

Auto-group shopping list items by Kroger aisle/department so users don't crisscross the store. Currently items are listed in the order they were added.

**UI sketch:** A "Sort by Aisle" toggle above the shopping list table. When active, section headers like "Produce", "Dairy", "Meat & Seafood" appear, with items grouped underneath. Falls back to "Unknown aisle" when Kroger API doesn't return aisle data.

### 5. In-store mode (phone-optimized)
**Who:** Mobile shoppers | **Effort:** S | **Priority:** P1

One-tap view that strips navigation, expands touch targets, and shows a simple check-off list with aisle labels. The current UI is desktop-first with standard Bootstrap sizing.

**UI sketch:** "Store Mode" button in the shopping list header. Toggles to a full-screen card-per-item layout with large check circles and swipe-to-remove gesture.

### 6. Barcode scanner for shopping list
**Who:** Precision shoppers | **Effort:** L | **Priority:** P2

Scan a product barcode to instantly add it to the shopping list with the correct Kroger product (no search + selection needed). Requires camera API permission flow.

**UI sketch:** Small camera icon in the "Add to Shopping List" search area. Opens an inline viewfinder rectangle. On scan, product name appears with an "Add" button.

### 7. Compare across Kroger locations
**Who:** Deal hunters | **Effort:** L | **Priority:** P2

Show prices for the same items at nearby Kroger stores so users can pick the cheapest location. Currently there's one fixed preferred location.

**UI sketch:** "Compare Stores" link next to the location label. Opens a modal with a stripped-down table: item → price at Store A → price at Store B.

---

## Recipe Discovery & Cooking

### 8. "Cook from pantry" suggestions
**Who:** "What's for dinner?" users | **Effort:** M | **Priority:** P0

Given current pantry contents, show recipes that use what you already have. Filters by "have ≥80% of ingredients", sorted by fewest missing items. Currently there's no connection between the pantry and recipe discovery.

**UI sketch:** "Cook from Pantry" card on the dashboard and a new tab on the recipes page. Each card shows pantry-match percentage and missing ingredients in red.

### 9. Dietary restriction profile + filters
**Who:** Health-directed eaters | **Effort:** M | **Priority:** P0

Persistent user settings for dietary patterns (keto, vegan, gluten-free, low-FODMAP, etc.) that automatically filter recipes and flag conflicts in meal plans. Currently dietary concerns are surfaced only through the per-recipe safety badge.

**UI sketch:** Settings → "Dietary Preferences" section with toggles/checkboxes. Recipe cards get a subtle badge when they match ("Keto ✅") and a warning when they don't ("Contains gluten"). Meal plan slots highlight conflicts.

### 10. Cooking mode for recipes
**Who:** Users cooking at the stove | **Effort:** M | **Priority:** P1

Full-screen, high-contrast step view with one-handed tap-to-advance, built-in timers per step, and text that doesn't time out. Currently the recipe page is a scrollable document.

**UI sketch:** "Start Cooking" button below the recipe title. Fires a full-screen overlay: ingredient list at the top, current step in large centered text, a "Next Step →" button. Timers show countdown circles. Exits with Esc or "Done".

### 11. Ingredient substitution suggestions
**Who:** Users missing an ingredient | **Effort:** L | **Priority:** P2

When a recipe ingredient has no good Kroger match (or is out of stock nearby), suggest 1–3 substitutes with a note on how the dish changes. Currently the system just flags "not found" and moves on.

**UI sketch:** Yellow warning pill next to the ingredient: "Substitute: Greek yogurt → sour cream (tangier but works)". Tap it to swap the ingredient in the list.

### 12. Recipe print / PDF export
**Who:** Binder-and-print cooks | **Effort:** S | **Priority:** P2

Clean, printer-friendly layout for any recipe view — no nav, no sidebars, no large images, just ingredients and steps. Bonus: PDF download.

**UI sketch:** "Print" icon in the recipe detail header. Triggers `window.print()` with a print-specific stylesheet.

---

## Pantry & Inventory

### 13. Pantry expiration tracking + alerts
**Who:** Waste-reduction users | **Effort:** M | **Priority:** P1

Add optional expiration dates to pantry items. Show items expiring within 7 days with a badge, and suggest recipes that use them. Currently pantry tracks level % only.

**UI sketch:** Optional date input in the "Add Pantry Item" modal. On the pantry page, items with ≤7 days to expiry get an orange "Expiring" badge with the date. Dashboard can show an "Expiring Soon" card.

### 14. Auto-deduct pantry when cooking
**Who:** Accurate-inventory users | **Effort:** L | **Priority:** P2

When a recipe is "cooked" (marked done via tracker), automatically decrease pantry levels for ingredients used. Currently pantry is fully manual — level changes require editing each item.

**UI sketch:** "I Cooked This" button on recipe detail / in cooking mode. After confirmation, subtracts standard portion amounts from matching pantry items. Creates a "Cooked on <date>" entry in meal tracker.

---

## Nutrition & Health

### 15. Per-recipe nutritional breakdown
**Who:** Macros-trackers | **Effort:** M | **Priority:** P1

Show approximate calories, protein, fat, carbs per serving for each recipe. The health badge currently flags concerns but doesn't show a full nutritional profile.

**UI sketch:** "Nutrition" collapsible section on the recipe detail page, below ingredients. Shows a small bar-chart row: calories | protein | fat | carbs. Data sourced from ingredient-level estimates (open food databases).

### 16. Batch safety scan for recipes
**Who:** Health-optimized shoppers | **Effort:** S | **Priority:** P1

Currently you scan ingredients one-by-one via `safety(action='check_product')`. A batch mode would scan all recipe ingredients at once and show a unified report card.

**UI sketch:** "Check All Ingredients" button on recipe detail. Opens a modal with a green/yellow/red row per ingredient. Top banner: "7/8 ingredients passed — 1 flagged (artificial color in ingredient #3)"

---

## Sharing & Collaboration

### 17. Shared / collaborative shopping lists
**Who:** Households with multiple shoppers | **Effort:** L | **Priority:** P2

Share a shopping list with family members. Changes sync in near-real-time. Each person checks off items as they grab them. Currently the list is single-user.

**UI sketch:** "Share" button in the shopping list header. Opens a dialog with a shareable link (auth-gated) or email invite. Shared users see avatars of who checked what.

### 18. Favorite recipe collections / cookbooks
**Who:** Recipe collectors | **Effort:** S | **Priority:** P1

The favorites page already supports lists — extend it to be a first-class "cookbook" with cover images, descriptions, and the ability to generate a shopping list from an entire collection.

**UI sketch:** Favorites list gets an optional "cover recipe" picker and a description field. List cards show a mini cover + recipe count + "Add All to List" button.

---

## Analytics & Insights

### 19. Monthly spending report
**Who:** Budget-trackers | **Effort:** M | **Priority:** P1

The analytics page exists but doesn't have a monthly spending breakdown with category charts (produce, dairy, meat, pantry). Users want to see "I spent $340 on groceries this month, $80 of it on meat."

**UI sketch:** "Monthly Spending" card in analytics with a stacked bar chart, a category pie chart, and a comparison to the previous month. Filter by date range.

### 20. "Saved vs. spent" with deals tracking
**Who:** Deal-conscious shoppers | **Effort:** L | **Priority:** P2

Track how much users saved via weekly deals, digital coupons, and Kroger card discounts. Compare to full-price equivalents.

**UI sketch:** "This Week's Savings" card on the dashboard: "You saved $12.40 on 5 deal items." Links to a detailed deals history page with per-trip savings.
