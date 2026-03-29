# Smart Shopper — Comprehensive Frontend Layout Reference

> Generated for Playwright E2E testing. Documents EVERY page, button, feature, and interactive element.

---

## Design System: "Verdant Editorial"

- **Fonts**: Lora (serif, headings), DM Sans (sans-serif, body)
- **Color Space**: OKLCH
- **Primary Colors**:
  - Cream background: `oklch(97% 0.012 80)`
  - Surface/cards: `oklch(99.5% 0.005 80)`
  - Border: `oklch(88% 0.018 80)`
  - Green (primary action): `oklch(48% 0.14 148)`
  - Amber (active nav): `oklch(72% 0.165 72)`
  - Sidebar bg: `oklch(21% 0.065 155)` (dark green)
- **Frameworks**: TailwindCSS (CDN), Alpine.js 3.x

---

## Global Layout

### Sidebar (left, fixed, 224px / w-56)
- **Brand**: "Smart Shopper" with cart icon (amber bg)
- **Nav Groups**:
  - *(ungrouped)*: Dashboard
  - **Shop**: Products, List (cart), Shopping List
  - **Manage**: Pantry, Favorites, Recipes
  - **Plan**: Meal Plan, Predictions, Analytics
  - **Config**: Safety, Settings
- **Active state**: Amber background, dark text, bold
- **Hover state**: Lighter green-tinted background

### Header (top bar, 56px)
- Page title (Lora serif font)

### Main Content Area
- Left margin 224px (sidebar width)
- Padding 2rem (32px)

---

## Pages

### 1. Dashboard (`/dashboard`)
- **Active page**: `dashboard`
- **Title**: "Dashboard"
- **Content**:
  - Pantry alerts widget (items needing attention)
  - Weekly meal overview
  - Overdue favorites badges
  - Stats cards: recipe count, pantry alerts, active meal plans, favorites lists

### 2. Products (`/products`)
- **Active page**: `products`
- **Title**: "Products"
- **Features**:
  - Search bar (text input + search button)
  - Product result cards with: image, name, brand, price (regular/sale), safety grade badge
  - "Add to Cart" button per product
  - "Add to Watchlist" button per product
  - Watchlist section showing tracked items
  - Price history view per product

### 3. Cart / List (`/cart`)
- **Active page**: `cart`
- **Title**: "Cart"
- **Features**:
  - Current cart items list (description, quantity, price, remove button)
  - Cart summary (count, total price)
  - "Clear Cart" button (with confirmation)
  - "Mark Placed" button (with confirmation, triggers Kroger sync)
  - Order history section (last 10 orders with date, item count)

### 4. Shopping List (`/shopping-list`)
- **Active page**: `shopping_list`
- **Title**: "Shopping List"
- **Features**:
  - Recipe dropdown selector + servings override + "Add Recipe" button
  - Items list with: name, quantity, source recipe, notes, remove button
  - "Preview Cart" button (shows what will be added/skipped)
  - "Add to Cart" button (with confirmation)
  - "Clear List" button

### 5. Pantry (`/pantry`)
- **Active page**: `pantry`
- **Title**: "Pantry"
- **Features**:
  - Items grouped by status: Out of Stock, Low Stock, OK
  - Each item: name, level bar (0-100%), restock button, remove button
  - "Add Item" form (search + initial level)
  - "Clear All Pantry" button (requires ?confirmed=true)
  - Expiring soon section

### 6. Favorites (`/favorites`)
- **Active page**: `favorites`
- **Title**: "Favorites"
- **Features**:
  - Grid of favorites lists (name, item count, reorder status badge)
  - Click list → detail page `/favorites/{list_id}`

### 7. Favorites Detail (`/favorites/{list_id}`)
- **Active page**: `favorites`
- **Features**:
  - List header (name, description, reorder status)
  - Items with: product name, pantry level badge, quantity, "Add to Shopping" button, "Remove" button
  - "Add All to Shopping List" button

### 8. Recipes (`/recipes`)
- **Active page**: `recipes`
- **Title**: "Recipes"
- **Features**:
  - **Filter toolbar**:
    - Tag filter pills (multi-select)
    - Search by name
    - Cost/health filters
  - **Two-zone ranked sort UI**:
    - Zone A: Toggle buttons (Healthiest First, Cost, Times Ordered, Newest, etc.)
    - Zone B: Ranked chip strip with ordinals (1st, 2nd, 3rd...), promote/demote/remove buttons, "Clear all" button
  - **Recipe cards**: Name, cost, health grade badge (clickable popover), tags, times ordered
  - Click card → recipe detail

### 9. Recipe Detail (`/recipes/{recipe_id}`)
- **Active page**: `recipes`
- **Features**:
  - Recipe name, description, servings, tags
  - Overall health badge (Alpine-rendered)
  - **Ingredient panel**:
    - Segmented toggle: "As listed" / "By category"
    - Column headers: Qty, Ingredient, Source, Health
    - Per-ingredient rows with safety grade badges (clickable popovers)
    - Category bands with item counts (when "By category" active)
  - Cooking instructions (grouped by section)
  - "Add to Cart" button (preview + confirm flow)
  - "Edit" / "Delete" actions

### 10. Meal Plan (`/meal-plan`)
- **Active page**: `meal_plan`
- **Title**: "Meal Plan"
- **Features**:
  - Plan selector dropdown
  - Week navigation (prev/next buttons, week label)
  - 7-day × 4-slot meal calendar grid
  - Each slot: recipe name (clickable), "Cooked" checkbox
  - Action buttons: Create Plan, Copy Plan, Preview Shopping, Add to Cart
  - Sidebar stats: meal count, unique recipes, cooked count

### 11. Predictions (`/predictions`)
- **Active page**: `predictions`
- **Title**: "Predictions"
- **Features**:
  - Prediction list with: product name, category badge, predicted date, urgency badge, days until, confidence %, last purchase date
  - "Add to Cart" quick button per prediction

### 12. Analytics (`/analytics`)
- **Active page**: `analytics`
- **Title**: "Analytics"
- **Features**:
  - Date range selector (7/14/30/90 days)
  - Tabs: Spending, Patterns, Pantry Report, Cookable Recipes
  - Export button (JSON download)
  - Category breakdown tables

### 13. Safety (`/safety`)
- **Active page**: `safety`
- **Title**: "Safety"
- **Features**:
  - Settings: filtering toggle, block mode radio buttons, save button
  - Ingredients list: filterable by severity/category, search, each with severity badge
  - Custom ingredient: add/remove with modal (name, severity, category, reason, aliases)
  - Approved products section (add/remove)
  - Blocked products section (add/remove)

### 14. Settings (`/settings`)
- **Active page**: `settings`
- **Title**: "Settings"
- **Features**:
  - Location: current display, change button → ZIP search modal
  - Servings: current value, input field, save button
  - Auth status display

### 15. Login (`/login`)
- **Standalone page** (no sidebar)
- Auth card: email input, password input, "Sign In" button
- Error message display
- "Create one" link → /register

### 16. Register (`/register`)
- **Standalone page** (no sidebar)
- Auth card: display name, email, password, confirm password, "Create Account" button
- Error message display
- "Sign in" link → /login

---

## API Endpoints (for testing responses)

### Cart API (`/api/cart`)
- `POST /api/cart` — Add item
- `GET /api/cart` — Get current cart
- `DELETE /api/cart/{product_id}` — Remove item
- `DELETE /api/cart` — Clear cart
- `POST /api/cart/mark-placed` — Mark order placed
- `GET /api/cart/history` — Order history

### Pantry API (`/api/pantry`)
- `POST /api/pantry/update` — Update level
- `POST /api/pantry/add` — Add item
- `DELETE /api/pantry?confirmed=true` — Clear all
- `DELETE /api/pantry/{product_id}` — Remove item
- `POST /api/pantry/restock` — Restock item

### Products API (`/api/products`)
- `GET /api/products/search?q=X` — Search
- `POST /api/products/{id}/add-to-cart` — Add to cart

### Shopping List API (`/api/shopping-list`)
- `GET /api/shopping-list` — Get list
- `POST /api/shopping-list/add-recipe` — Add recipe
- `DELETE /api/shopping-list/{id}` — Remove item
- `POST /api/shopping-list/add-to-cart` — Transfer to cart

### Favorites API (`/api/favorites`)
- `GET /api/favorites/lists` — Get lists
- `POST /api/favorites/lists` — Create list
- `DELETE /api/favorites/lists/{id}` — Delete list
- `GET /api/favorites/lists/{id}/items` — Get items
- `POST /api/favorites/lists/{id}/items` — Add item
- `DELETE /api/favorites/lists/{id}/items/{pid}` — Remove item

### Recipes API (`/api/recipes`)
- `DELETE /api/recipes/{id}` — Delete recipe
- `PATCH /api/recipes/{id}` — Update recipe
- `POST /api/recipes/{id}/add-to-cart` — Add to cart

### Meal Plan API (`/api/meal-plan`)
- `POST /api/meal-plan` — Create plan
- `DELETE /api/meal-plan/{id}` — Delete plan
- `POST /api/meal-plan/{id}/meals` — Add meal
- `DELETE /api/meal-plan/{id}/meals` — Remove meal
- `GET /api/meal-plan/list` — List plans
- `GET /api/meal-plan/{id}/week?week_offset=N` — Get week data

### Analytics API (`/api/analytics`)
- `GET /api/analytics/spending?days=N` — Spending report
- `GET /api/analytics/patterns?days=N` — Patterns
- `GET /api/analytics/pantry-report` — Pantry report
- `GET /api/analytics/cookable-recipes` — Cookable recipes
- `GET /api/analytics/export` — JSON export

### Safety API (`/api/safety`)
- `GET /api/safety/settings` — Get settings
- `POST /api/safety/settings` — Update settings
- `GET /api/safety/ingredients` — Get ingredients
- `GET /api/safety/approved` — Approved products
- `GET /api/safety/blocked` — Blocked products

### Settings API (`/api/settings`)
- `GET /api/settings` — Get all settings
- `POST /api/settings/servings` — Update servings
- `POST /api/settings/location` — Update location

### Predictions API (`/api/predictions`)
- `GET /api/predictions?days=N` — Get predictions
- `GET /api/predictions/smart` — Smart recommendations

### Deals API (`/api/deals`)
- `GET /api/deals/auto` — Auto deals
- `GET /api/deals/find?q=X` — Search deals
- `GET /api/deals/watchlist` — Get watchlist

---

## Visual Quality Checklist

### Per Page
- [ ] Page loads without JS errors
- [ ] Sidebar shows correct active state (amber highlight)
- [ ] Page title displays in Lora serif font
- [ ] Cards have consistent border-radius (0.75rem) and borders
- [ ] Buttons use green primary color with white text
- [ ] Text hierarchy: headings darker/larger, body text readable
- [ ] No layout overflow or broken alignment
- [ ] Responsive behavior (if applicable)

### Color Scheme Consistency
- [ ] Cream background throughout
- [ ] Green for primary actions/links
- [ ] Amber for active navigation only
- [ ] Grade badges use appropriate colors (green=A, yellow=B/C, red=D/F)
- [ ] Status badges use semantic colors (red=critical, yellow=warning, green=ok)

### Typography Consistency
- [ ] Lora serif for: brand, page titles, section headings
- [ ] DM Sans for: body text, buttons, labels, form inputs
- [ ] Consistent font sizing across pages
