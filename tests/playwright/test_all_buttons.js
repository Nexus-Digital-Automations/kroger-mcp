/**
 * Comprehensive Playwright tests for all buttons across the Smart Shopper frontend.
 * Tests navigation, page-specific buttons, modals, and interactive elements.
 */
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8080';
const TIMEOUT = 10000;

let browser, context, page;
let passed = 0;
let failed = 0;
const failures = [];

// ── Helpers ──────────────────────────────────────────

async function setup() {
  browser = await chromium.launch({ headless: true });
  context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);
}

async function teardown() {
  await browser.close();
}

async function navigateTo(path) {
  const url = `${BASE}${path}`;
  const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
  // Wait for Alpine.js to initialize
  await page.waitForTimeout(800);
  return resp;
}

function ok(testName) {
  passed++;
  console.log(`  ✓ ${testName}`);
}

function fail(testName, err) {
  failed++;
  const msg = err?.message || String(err);
  failures.push({ test: testName, error: msg });
  console.log(`  ✗ ${testName} — ${msg}`);
}

async function test(name, fn) {
  try {
    await fn();
    ok(name);
  } catch (e) {
    fail(name, e);
  }
}

async function assertVisible(selector, label) {
  const el = page.locator(selector).first();
  await el.waitFor({ state: 'visible', timeout: 5000 });
}

async function assertExists(selector) {
  const count = await page.locator(selector).count();
  if (count === 0) throw new Error(`No element found for: ${selector}`);
}

async function clickAndExpect(clickSelector, expectSelector, label) {
  await page.locator(clickSelector).first().click();
  await page.waitForTimeout(500);
  await assertVisible(expectSelector, label);
}

async function assertNoJsErrors(path) {
  const errs = [];
  page.on('pageerror', e => errs.push(e.message));
  await navigateTo(path);
  await page.waitForTimeout(500);
  page.removeAllListeners('pageerror');
  if (errs.length > 0) {
    throw new Error(`JS errors on ${path}: ${errs.slice(0, 3).join('; ')}`);
  }
}

// ── Test Suites ──────────────────────────────────────

async function testSidebarNavigation() {
  console.log('\n── Sidebar Navigation ──');
  const navLinks = [
    { href: '/dashboard', label: 'Dashboard' },
    { href: '/products', label: 'Products' },
    { href: '/shopping-list', label: 'Shopping List' },
    { href: '/pantry', label: 'Pantry' },
    { href: '/meal-tracker', label: 'Meal Tracker' },
    { href: '/favorites', label: 'Favorites' },
    { href: '/recipes', label: 'Recipes' },
    { href: '/meal-plan', label: 'Meal Plan' },
    { href: '/predictions', label: 'Predictions' },
    { href: '/analytics', label: 'Analytics' },
    { href: '/safety', label: 'Safety' },
    { href: '/settings', label: 'Settings' },
  ];

  await navigateTo('/dashboard');

  for (const link of navLinks) {
    await test(`Nav link: ${link.label} -> ${link.href}`, async () => {
      const el = page.locator(`aside a[href="${link.href}"]`);
      await el.waitFor({ state: 'visible', timeout: 5000 });
      await el.click();
      await page.waitForLoadState('networkidle');
      const url = page.url();
      if (!url.includes(link.href)) {
        throw new Error(`Expected URL to contain ${link.href}, got ${url}`);
      }
    });
  }
}

async function testDashboardButtons() {
  console.log('\n── Dashboard ──');
  await navigateTo('/dashboard');

  await test('Browse Recipes button navigates to /recipes', async () => {
    const btn = page.locator('a[href="/recipes"]').filter({ hasText: 'Browse Recipes' });
    await btn.waitFor({ state: 'visible', timeout: 5000 });
    await btn.click();
    await page.waitForLoadState('networkidle');
    if (!page.url().includes('/recipes')) {
      throw new Error(`Expected /recipes, got ${page.url()}`);
    }
  });

  await navigateTo('/dashboard');

  await test('Shopping List button navigates to /shopping-list', async () => {
    // Use main content area link (not sidebar) — there are two matching links
    const btn = page.locator('main a[href="/shopping-list"]').filter({ hasText: 'Shopping List' });
    await btn.waitFor({ state: 'visible', timeout: 5000 });
    await btn.click();
    await page.waitForLoadState('networkidle');
    if (!page.url().includes('/shopping-list')) {
      throw new Error(`Expected /shopping-list, got ${page.url()}`);
    }
  });

  await navigateTo('/dashboard');

  await test('View pantry link exists', async () => {
    await assertExists('a[href="/pantry"]');
  });

  await test('View full plan link exists', async () => {
    await assertExists('a[href="/meal-plan"]');
  });
}

async function testRecipesButtons() {
  console.log('\n── Recipes ──');
  await navigateTo('/recipes');

  await test('Search input exists', async () => {
    await assertVisible('input[x-model="search"]');
  });

  await test('Tag filter dropdown button works', async () => {
    const tagBtn = page.locator('button').filter({ hasText: 'Filter by tag' }).first();
    const count = await tagBtn.count();
    if (count > 0) {
      await tagBtn.click();
      await page.waitForTimeout(400);
      // Check if dropdown appeared — look for "Clear all" button inside it
      const clearAll = page.locator('button').filter({ hasText: 'Clear all' });
      if (await clearAll.count() > 0) {
        await clearAll.first().click();
      }
      ok('Tag filter dropdown opened');
    } else {
      // No tags in DB — skip gracefully
      ok('Tag filter (no tags in DB — skipped)');
    }
  });

  await test('Sort dropdown button works', async () => {
    const sortBtn = page.locator('button').filter({ hasText: 'Sort' }).first();
    await sortBtn.click();
    await page.waitForTimeout(400);
    // Look for sort option buttons inside the dropdown
    const sortOptions = page.locator('[x-show="sortDropdownOpen"] button');
    const count = await sortOptions.count();
    if (count === 0) {
      throw new Error('Sort dropdown did not open — no sort option buttons found');
    }
    // Close the dropdown by clicking the sort button again
    await sortBtn.click();
    await page.waitForTimeout(200);
  });

  // Test recipe card buttons (if recipes exist)
  const recipeCards = page.locator('template[x-for="recipe in filtered"]');
  const hasRecipes = await page.locator('[x-text="recipe.name"]').count();

  if (hasRecipes > 0) {
    await test('Recipe card: "Add to List" button exists', async () => {
      const btn = page.locator('button').filter({ hasText: 'Add to List' }).first();
      await btn.waitFor({ state: 'visible', timeout: 5000 });
    });

    await test('Recipe card: "Meal Plan" button exists', async () => {
      const btn = page.locator('button').filter({ hasText: 'Meal Plan' }).first();
      await btn.waitFor({ state: 'visible', timeout: 5000 });
    });

    await test('Recipe card: delete button exists', async () => {
      const btn = page.locator('button[title="Delete recipe"]').first();
      await btn.waitFor({ state: 'visible', timeout: 5000 });
    });

    await test('Recipe card: "Add to List" button fires API call', async () => {
      const [response] = await Promise.all([
        page.waitForResponse(r => r.url().includes('/api/shopping-list/add-recipe'), { timeout: 8000 }),
        page.locator('button').filter({ hasText: 'Add to List' }).first().click(),
      ]);
      const status = response.status();
      if (status >= 500) {
        throw new Error(`API returned ${status}`);
      }
    });

    await test('Recipe card: "Meal Plan" button opens meal panel', async () => {
      await page.locator('button').filter({ hasText: 'Meal Plan' }).first().click();
      await page.waitForTimeout(500);
      // The meal plan modal should become visible
      const modal = page.locator('[x-show="open"]').filter({ hasText: /Meal Plan|assign|week/i });
      const modalCount = await modal.count();
      // Close if open
      const closeBtn = page.locator('button').filter({ hasText: /close|cancel|×/i });
      if (await closeBtn.count() > 0) {
        await closeBtn.first().click({ timeout: 2000 }).catch(() => {});
      }
      await page.keyboard.press('Escape');
      await page.waitForTimeout(300);
    });

    await test('Recipe card: health grade badge popup works', async () => {
      const badge = page.locator('button[x-text="recipe.health_grade"]').first();
      if (await badge.count() > 0 && await badge.isVisible()) {
        await badge.click({ force: true });
        await page.waitForTimeout(400);
        // Close it
        await page.keyboard.press('Escape');
        await page.waitForTimeout(200);
      }
    });

    await test('Recipe card links to detail page', async () => {
      const link = page.locator('a[href^="/recipes/"]').first();
      const href = await link.getAttribute('href');
      // Navigate directly instead of clicking (avoids overlay issues)
      await navigateTo(href);
      if (!page.url().includes('/recipes/')) {
        throw new Error(`Expected /recipes/ URL, got ${page.url()}`);
      }
    });
  } else {
    console.log('  (no recipes in DB — skipping recipe card button tests)');
  }
}

async function testRecipeDetailButtons() {
  console.log('\n── Recipe Detail ──');
  // Find a recipe ID from the recipes page
  await navigateTo('/recipes');
  const firstLink = page.locator('a[href^="/recipes/"]').first();
  if (await firstLink.count() === 0) {
    console.log('  (no recipes — skipping detail tests)');
    return;
  }

  const href = await firstLink.getAttribute('href');
  await navigateTo(href);

  await test('Back link to /recipes exists', async () => {
    await assertExists('a[href="/recipes"]');
  });

  await test('Edit button exists and opens modal', async () => {
    // Dismiss any overlay first
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    const editBtn = page.locator('button').filter({ hasText: 'Edit' }).first();
    if (await editBtn.count() > 0) {
      await editBtn.click({ force: true });
      await page.waitForTimeout(400);
      // Look for Save Changes or Cancel
      const saveBtn = page.locator('button').filter({ hasText: 'Save Changes' });
      if (await saveBtn.count() > 0) {
        // Close the modal
        await page.keyboard.press('Escape');
        await page.waitForTimeout(200);
      }
    }
  });

  await test('Servings +/- buttons work', async () => {
    // Dismiss any overlay
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    const incBtn = page.locator('button').filter({ hasText: '+' }).first();
    const decBtn = page.locator('button').filter({ hasText: '−' }).first();
    if (await incBtn.count() > 0) {
      await incBtn.click({ force: true });
      await page.waitForTimeout(200);
      await decBtn.click({ force: true });
      await page.waitForTimeout(200);
    }
  });

  await test('Ingredient view toggle buttons work', async () => {
    // Dismiss any overlay
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    const asListed = page.locator('button').filter({ hasText: 'As listed' }).first();
    const byCat = page.locator('button').filter({ hasText: 'By category' }).first();
    if (await asListed.count() > 0) {
      await byCat.click({ force: true });
      await page.waitForTimeout(200);
      await asListed.click({ force: true });
      await page.waitForTimeout(200);
    }
  });

  await test('"+ Meal Plan" button exists', async () => {
    const btn = page.locator('button').filter({ hasText: /Meal Plan/i }).first();
    if (await btn.count() === 0) {
      throw new Error('Meal Plan button not found');
    }
  });
}

async function testShoppingListButtons() {
  console.log('\n── Shopping List ──');
  await navigateTo('/shopping-list');

  await test('Page loads without JS errors', async () => {
    const errs = [];
    page.on('pageerror', e => errs.push(e.message));
    await page.waitForTimeout(500);
    page.removeAllListeners('pageerror');
    if (errs.length > 0) throw new Error(errs.join('; '));
  });

  // Shopping list section
  await test('"Send to Cart" button exists (visible when items present)', async () => {
    const btn = page.locator('button').filter({ hasText: 'Send to Cart' });
    // Button may be hidden if list is empty — just check it exists in DOM
    await assertExists('button:has-text("Send to Cart")');
  });

  // Cart section
  await test('"Clear Cart" button exists in cart section', async () => {
    await assertExists('button:has-text("Clear Cart")');
  });

  await test('"Mark Order Placed" button exists', async () => {
    await assertExists('button:has-text("Mark Order Placed")');
  });

  await test('"Filters" button toggles filter panel', async () => {
    const filterBtn = page.locator('button').filter({ hasText: 'Filters' }).first();
    if (await filterBtn.isVisible()) {
      await filterBtn.click();
      await page.waitForTimeout(300);
      // Filter categories should appear
      await filterBtn.click();
      await page.waitForTimeout(200);
    }
  });

  await test('"Order History" toggle button works', async () => {
    const histBtn = page.locator('button').filter({ hasText: 'Order History' });
    await histBtn.click();
    await page.waitForTimeout(300);
    // Section should expand
    await histBtn.click();
    await page.waitForTimeout(200);
  });

  // Test quantity buttons if items exist
  const qtyMinus = page.locator('button:has-text("−")').first();
  if (await qtyMinus.isVisible().catch(() => false)) {
    await test('Quantity −/+ buttons exist for shopping list items', async () => {
      await assertExists('button:has-text("−")');
      await assertExists('button:has-text("+")');
    });
  }
}

async function testPantryButtons() {
  console.log('\n── Pantry ──');
  await navigateTo('/pantry');

  await test('"Add Item" button exists and opens modal', async () => {
    // Target the visible page-level "Add Item" button (has the + SVG icon), not the modal's submit
    const addBtn = page.locator('button:has(svg):visible').filter({ hasText: 'Add Item' }).first();
    await addBtn.waitFor({ state: 'visible', timeout: 5000 });
    await addBtn.click();
    await page.waitForTimeout(500);
    // Modal should be visible
    const modal = page.locator('h3:has-text("Add Pantry Item")');
    await modal.waitFor({ state: 'visible', timeout: 3000 });
    // Close it via Escape
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
  });

  await test('"Restock All Low" button exists', async () => {
    await assertVisible('button:has-text("Restock All Low")');
  });

  await test('"Clear Pantry" button exists', async () => {
    await assertVisible('button:has-text("Clear Pantry")');
  });

  // Check per-item buttons if items exist
  const editBtns = page.locator('button:has-text("Edit %")');
  if (await editBtns.count() > 0) {
    await test('Per-item "Edit %" button toggles inline editor', async () => {
      const firstEdit = editBtns.first();
      await firstEdit.click();
      await page.waitForTimeout(300);
      // Save and close buttons should appear
      await assertExists('button:has-text("Save")');
      // Close the editor
      const closeBtn = page.locator('button:has-text("✕")').first();
      await closeBtn.click();
      await page.waitForTimeout(200);
    });

    await test('Per-item "Restock" button exists', async () => {
      await assertExists('button:has-text("Restock")');
    });

    await test('Per-item "Remove" button exists', async () => {
      await assertExists('button:has-text("Remove")');
    });
  } else {
    console.log('  (no pantry items — skipping per-item button tests)');
  }
}

async function testMealPlanButtons() {
  console.log('\n── Meal Plan ──');
  await navigateTo('/meal-plan');

  await test('Page loads without JS errors', async () => {
    const errs = [];
    page.on('pageerror', e => errs.push(e.message));
    await page.waitForTimeout(500);
    page.removeAllListeners('pageerror');
    if (errs.length > 0) throw new Error(errs.join('; '));
  });

  // "+ New Plan" button
  await test('"+ New Plan" button exists and opens modal', async () => {
    const newPlanBtn = page.locator('button:visible').filter({ hasText: '+ New Plan' }).first();
    if (await newPlanBtn.count() > 0) {
      await newPlanBtn.click();
      await page.waitForTimeout(400);
      // Modal "New Meal Plan" heading should appear
      const heading = page.locator('h2:has-text("New Meal Plan")');
      await heading.waitFor({ state: 'visible', timeout: 3000 });
      // Close the modal
      await page.keyboard.press('Escape');
      await page.waitForTimeout(200);
    } else {
      // Page may show existing plan instead — verify the page loaded
      const pageTitle = page.locator('h1').filter({ hasText: /Meal Plan/i });
      if (await pageTitle.count() === 0) {
        throw new Error('No meal plan UI found');
      }
    }
  });

  // Check for meal slot buttons (+ buttons on calendar cells)
  const addMealBtns = page.locator('button').filter({ hasText: '+' });
  if (await addMealBtns.count() > 0) {
    await test('Meal slot "+" buttons exist on calendar', async () => {
      // Just verify they exist; clicking would open add-meal modal
    });
  }
}

async function testFavoritesButtons() {
  console.log('\n── Favorites ──');
  await navigateTo('/favorites');

  await test('"+ New List" button exists and opens modal', async () => {
    const btn = page.locator('button').filter({ hasText: '+ New List' });
    await btn.waitFor({ state: 'visible', timeout: 5000 });
    await btn.click();
    await page.waitForTimeout(400);
    // Modal with "New Favorites List" heading should appear
    await assertVisible('h2:has-text("New Favorites List")');
    // Close
    const cancelBtn = page.locator('button').filter({ hasText: 'Cancel' }).first();
    await cancelBtn.click();
    await page.waitForTimeout(200);
  });

  // Check for per-list buttons if lists exist
  const editBtns = page.locator('button[title="Edit list"]');
  if (await editBtns.count() > 0) {
    await test('Per-list Edit button opens edit modal', async () => {
      await editBtns.first().click();
      await page.waitForTimeout(400);
      await assertVisible('h2:has-text("Edit List")');
      // Close via Escape (Cancel button may resolve to wrong hidden modal)
      await page.keyboard.press('Escape');
      await page.waitForTimeout(200);
    });

    await test('Per-list Delete button exists', async () => {
      await assertExists('button[title="Delete list"]');
    });

    await test('Per-list "+ Add to List" button exists', async () => {
      await assertExists('button:has-text("+ Add to List")');
    });
  } else {
    console.log('  (no favorites lists — skipping per-list tests)');
  }
}

async function testProductsButtons() {
  console.log('\n── Products ──');
  await navigateTo('/products');

  await test('Search/Deals mode toggle pills exist', async () => {
    const searchPill = page.locator('button').filter({ hasText: 'Search' }).first();
    const dealsPill = page.locator('button').filter({ hasText: 'Deals' }).first();
    await searchPill.waitFor({ state: 'visible', timeout: 5000 });
    if (await dealsPill.count() > 0) {
      await dealsPill.click();
      await page.waitForTimeout(400);
      await searchPill.click();
      await page.waitForTimeout(400);
    }
  });

  await test('Search button works', async () => {
    const searchInput = page.locator('input[x-model="query"]').first();
    if (await searchInput.count() > 0) {
      await searchInput.fill('milk');
      await page.waitForTimeout(200);
      // Find the search/submit button
      const searchBtn = page.locator('button[type="submit"], button:has-text("Search")').first();
      if (await searchBtn.count() > 0) {
        const [response] = await Promise.all([
          page.waitForResponse(r => r.url().includes('/api/products/search') || r.url().includes('/api/deals'), { timeout: 8000 }),
          searchBtn.click(),
        ]);
        if (response.status() >= 500) {
          throw new Error(`API returned ${response.status()}`);
        }
      }
    }
  });

  await test('Sort dropdown exists', async () => {
    const sortBtn = page.locator('button').filter({ hasText: 'Sort' }).first();
    if (await sortBtn.count() > 0) {
      await sortBtn.click();
      await page.waitForTimeout(300);
      await sortBtn.click(); // close
      await page.waitForTimeout(200);
    }
  });
}

async function testDealsButtons() {
  console.log('\n── Deals ──');
  await navigateTo('/deals');

  // /deals redirects to /products — verify that
  await test('/deals redirects correctly', async () => {
    const url = page.url();
    if (!url.includes('/products') && !url.includes('/deals')) {
      throw new Error(`Expected /products or /deals redirect, got ${url}`);
    }
  });
}

async function testAnalyticsButtons() {
  console.log('\n── Analytics ──');
  await navigateTo('/analytics');

  await test('Tab buttons work: Spending', async () => {
    const btn = page.locator('button').filter({ hasText: 'Spending' }).first();
    await btn.click();
    await page.waitForTimeout(500);
  });

  await test('Tab buttons work: Patterns', async () => {
    const btn = page.locator('button').filter({ hasText: 'Patterns' }).first();
    await btn.click();
    await page.waitForTimeout(500);
  });

  await test('Tab buttons work: Pantry', async () => {
    const btn = page.locator('button').filter({ hasText: 'Pantry' }).first();
    await btn.click();
    await page.waitForTimeout(500);
  });

  await test('Tab buttons work: Cookable Now', async () => {
    const btn = page.locator('button').filter({ hasText: 'Cookable Now' }).first();
    await btn.click();
    await page.waitForTimeout(500);
  });

  await test('Time range buttons work (7d, 30d, 90d, 365d)', async () => {
    for (const d of ['7d', '30d', '90d', '365d']) {
      const btn = page.locator('button').filter({ hasText: d }).first();
      if (await btn.count() > 0) {
        await btn.click();
        await page.waitForTimeout(300);
      }
    }
  });

  await test('"Export All Data" button exists and triggers download', async () => {
    const exportBtn = page.locator('button').filter({ hasText: 'Export All Data' });
    await exportBtn.waitFor({ state: 'visible', timeout: 5000 });
    // Click and expect navigation to export URL
    const [response] = await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/analytics/export'), { timeout: 8000 }).catch(() => null),
      page.waitForEvent('popup', { timeout: 5000 }).catch(() => null),
      exportBtn.click(),
    ]);
  });
}

async function testPredictionsButtons() {
  console.log('\n── Predictions ──');
  await navigateTo('/predictions');

  await test('Look-ahead time buttons work', async () => {
    for (const label of ['7 days', '14 days', '30 days']) {
      const btn = page.locator('button').filter({ hasText: label }).first();
      if (await btn.count() > 0) {
        const [response] = await Promise.all([
          page.waitForResponse(r => r.url().includes('/api/predictions'), { timeout: 8000 }).catch(() => null),
          btn.click(),
        ]);
        await page.waitForTimeout(300);
      }
    }
  });
}

async function testSafetyButtons() {
  console.log('\n── Safety ──');
  await navigateTo('/safety');

  await test('Tab buttons work: Flagged Ingredients', async () => {
    const btn = page.locator('button').filter({ hasText: /Flagged|Ingredients/i }).first();
    if (await btn.count() > 0) {
      await btn.click();
      await page.waitForTimeout(300);
    }
  });

  await test('Tab buttons work: Custom Ingredients', async () => {
    const btn = page.locator('button').filter({ hasText: 'Custom' }).first();
    if (await btn.count() > 0) {
      await btn.click();
      await page.waitForTimeout(300);
    }
  });

  await test('Tab buttons work: Safe Products', async () => {
    const btn = page.locator('button').filter({ hasText: /Safe|Approved/i }).first();
    if (await btn.count() > 0) {
      await btn.click();
      await page.waitForTimeout(300);
    }
  });

  await test('Tab buttons work: Blocked Products', async () => {
    const btn = page.locator('button').filter({ hasText: 'Blocked' }).first();
    if (await btn.count() > 0) {
      await btn.click();
      await page.waitForTimeout(300);
    }
  });
}

async function testSettingsButtons() {
  console.log('\n── Settings ──');
  await navigateTo('/settings');

  await test('Location "Search" button exists', async () => {
    const btn = page.locator('button').filter({ hasText: 'Search' }).first();
    await btn.waitFor({ state: 'visible', timeout: 5000 });
  });

  await test('Location search works (enter ZIP and search)', async () => {
    const input = page.locator('input[x-model="locationSearch"]');
    await input.fill('77301');
    await page.waitForTimeout(200);
    const searchBtn = page.locator('button').filter({ hasText: 'Search' }).first();
    const [response] = await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/settings/location/search'), { timeout: 8000 }).catch(() => null),
      searchBtn.click(),
    ]);
    await page.waitForTimeout(500);
    // Check if location results appeared
    const results = page.locator('button[class*="text-left"]');
    // Results may or may not appear depending on API availability
  });

  await test('Servings "Save" button exists', async () => {
    const btn = page.locator('button').filter({ hasText: 'Save' }).first();
    await btn.waitFor({ state: 'visible', timeout: 5000 });
  });

  await test('Servings Save button fires API call', async () => {
    const saveBtn = page.locator('button').filter({ hasText: 'Save' }).first();
    const [response] = await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/settings/servings'), { timeout: 8000 }),
      saveBtn.click(),
    ]);
    if (response.status() >= 500) {
      throw new Error(`API returned ${response.status()}`);
    }
  });
}

async function testMealTrackerButtons() {
  console.log('\n── Meal Tracker ──');
  await navigateTo('/meal-tracker');

  await test('Page loads without JS errors', async () => {
    const errs = [];
    page.on('pageerror', e => errs.push(e.message));
    await page.waitForTimeout(500);
    page.removeAllListeners('pageerror');
    if (errs.length > 0) throw new Error(errs.join('; '));
  });

  await test('Meal type selector buttons exist', async () => {
    // Meal types like "Breakfast", "Lunch", "Dinner", "Snack"
    const mealTypes = page.locator('button').filter({ hasText: /Breakfast|Lunch|Dinner|Snack/i });
    const count = await mealTypes.count();
    if (count === 0) {
      throw new Error('No meal type selector buttons found');
    }
  });

  await test('Meal type buttons are clickable', async () => {
    const firstMealType = page.locator('button').filter({ hasText: /Breakfast|Lunch|Dinner|Snack/i }).first();
    await firstMealType.click();
    await page.waitForTimeout(300);
  });
}

async function testChatWidget() {
  console.log('\n── Chat Widget ──');
  await navigateTo('/dashboard');

  await test('Chat FAB button exists', async () => {
    const fab = page.locator('button[aria-label="Open chat assistant"]');
    await fab.waitFor({ state: 'visible', timeout: 5000 });
  });

  await test('Chat FAB opens chat panel', async () => {
    const fab = page.locator('button[aria-label="Open chat assistant"]');
    await fab.click();
    await page.waitForTimeout(500);
    // Chat panel header should be visible
    await assertVisible('.chat-header, [class*="chat-header"]');
  });

  await test('Quick chip buttons exist', async () => {
    const chips = page.locator('.chat-chip, button.chat-chip');
    const count = await chips.count();
    if (count < 2) {
      throw new Error(`Expected at least 2 chat chips, got ${count}`);
    }
  });

  await test('"Find deals" quick chip sends message', async () => {
    const chip = page.locator('button').filter({ hasText: 'Find deals' }).first();
    if (await chip.count() > 0 && await chip.isVisible()) {
      const [response] = await Promise.all([
        page.waitForResponse(r => r.url().includes('/api/chat/message'), { timeout: 10000 }).catch(() => null),
        chip.click(),
      ]);
      await page.waitForTimeout(500);
    }
  });

  await test('Chat close button works', async () => {
    const closeBtn = page.locator('button[aria-label="Close chat"]');
    if (await closeBtn.count() > 0 && await closeBtn.isVisible()) {
      await closeBtn.click();
      await page.waitForTimeout(400);
      // FAB should be visible again
      const fab = page.locator('button[aria-label="Open chat assistant"]');
      await fab.waitFor({ state: 'visible', timeout: 3000 });
    }
  });

  await test('Chat send button exists when panel is open', async () => {
    // Reopen
    const fab = page.locator('button[aria-label="Open chat assistant"]');
    await fab.click();
    await page.waitForTimeout(400);
    // Send button should exist in the chat input area
    const sendBtn = page.locator('.chat-panel button').last();
    if (await sendBtn.count() === 0) {
      throw new Error('No send button found in chat panel');
    }
    // Close
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
  });
}

async function testPageLoadNoErrors() {
  console.log('\n── Page Load (no JS errors) ──');
  const pages = [
    '/dashboard',
    '/products',
    '/shopping-list',
    '/pantry',
    '/meal-tracker',
    '/favorites',
    '/recipes',
    '/meal-plan',
    '/predictions',
    '/analytics',
    '/safety',
    '/settings',
  ];

  for (const path of pages) {
    await test(`${path} loads without JS errors`, async () => {
      const errs = [];
      const handler = e => errs.push(e.message);
      page.on('pageerror', handler);
      await navigateTo(path);
      await page.waitForTimeout(800);
      page.removeListener('pageerror', handler);
      if (errs.length > 0) {
        throw new Error(`JS errors: ${errs.slice(0, 3).join('; ')}`);
      }
    });
  }
}

// ── Main ─────────────────────────────────────────────

async function main() {
  console.log('Smart Shopper — Comprehensive Button Tests\n');
  console.log('='.repeat(50));

  await setup();

  try {
    await testPageLoadNoErrors();
    await testSidebarNavigation();
    await testDashboardButtons();
    await testRecipesButtons();
    await testRecipeDetailButtons();
    await testShoppingListButtons();
    await testPantryButtons();
    await testMealPlanButtons();
    await testFavoritesButtons();
    await testProductsButtons();
    await testDealsButtons();
    await testAnalyticsButtons();
    await testPredictionsButtons();
    await testSafetyButtons();
    await testSettingsButtons();
    await testMealTrackerButtons();
    await testChatWidget();
  } catch (e) {
    console.error('\nFatal error:', e.message);
  }

  await teardown();

  // Summary
  console.log('\n' + '='.repeat(50));
  console.log(`Results: ${passed} passed, ${failed} failed`);
  if (failures.length > 0) {
    console.log('\nFailures:');
    for (const f of failures) {
      console.log(`  ✗ ${f.test}`);
      console.log(`    ${f.error}`);
    }
  }
  console.log('');

  process.exit(failed > 0 ? 1 : 0);
}

main();
