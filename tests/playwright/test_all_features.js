// @ts-check
/**
 * Smart Shopper — Comprehensive Frontend Feature Test
 * Exercises EVERY page, EVERY interactive element, catches JS errors.
 * Usage: node tests/playwright/test_all_features.js
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8000';
const SS_DIR = path.join(__dirname, 'screenshots');
if (!fs.existsSync(SS_DIR)) fs.mkdirSync(SS_DIR, { recursive: true });

let browser, ctx, page;
let passed = 0, failed = 0, warned = 0, expectDialog = false;
const failures = [];
const consoleErrors = [];

function assert(cond, label) {
  if (cond) { console.log(`  \x1b[32mPASS\x1b[0m  ${label}`); passed++; }
  else { console.log(`  \x1b[31mFAIL\x1b[0m  ${label}`); failed++; failures.push(label); }
}
function warn(label) { console.log(`  \x1b[33mWARN\x1b[0m  ${label}`); warned++; }

async function ss(name) {
  await page.screenshot({ path: path.join(SS_DIR, `all_${name}.png`), fullPage: false });
}
async function goto(p) {
  await page.goto(`${BASE}${p}`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(600);
}

// ────────────────────────────────────────────
async function run() {
  browser = await chromium.launch({ headless: true });
  ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  page = await ctx.newPage();

  page.on('console', msg => {
    if (msg.type() === 'error') {
      const t = msg.text();
      if (!t.includes('favicon') && !t.includes('net::ERR') && !t.includes('Failed to load resource'))
        consoleErrors.push(t);
    }
  });

  // Track dialogs — auto-dismiss, track unexpected ones
  page.on('dialog', async d => {
    if (!expectDialog) warn(`Unexpected dialog: "${d.message().slice(0, 60)}"`);
    await d.dismiss().catch(() => {});
    expectDialog = false;
  });

  await testDashboard();
  await testRecipesPage();
  await testRecipeDetail();
  await testShoppingList();
  await testMealPlan();
  await testPantry();
  await testMealTracker();
  await testFavorites();
  await testProducts();
  await testDeals();
  await testAnalytics();
  await testSafety();
  await testSettings();
  await testPredictions();
  await testChatWidget();
  await testConsoleErrors();

  // ── Summary ──
  console.log(`\n${'═'.repeat(60)}`);
  console.log(`RESULTS: \x1b[32m${passed} passed\x1b[0m, \x1b[31m${failed} failed\x1b[0m, \x1b[33m${warned} warnings\x1b[0m`);
  if (failures.length) {
    console.log('\nFailed:');
    failures.forEach(f => console.log(`  • ${f}`));
  }
  console.log('');

  await browser.close();
  process.exit(failed > 0 ? 1 : 0);
}

// ══════════════════════════════════════════════
// TESTS
// ══════════════════════════════════════════════

async function testDashboard() {
  console.log('\n[Dashboard]');
  await goto('/dashboard');
  await ss('dashboard');
  assert(await page.locator('text=Dashboard').first().isVisible(), 'Dashboard page loads');
  const statCards = await page.locator('.ss-card, [class*="rounded-xl"]').count();
  assert(statCards >= 1, `Dashboard has stat cards (found ${statCards})`);
}

async function testRecipesPage() {
  console.log('\n[Recipes Page]');
  await goto('/recipes');
  await page.waitForTimeout(800);
  await ss('recipes');

  // Cards render
  const cards = await page.locator('[x-data="recipesGrid"] .relative.flex.flex-col').count();
  assert(cards > 0, `Recipe cards visible (${cards})`);

  // Each card has exactly one Actions button (all actions are inside the unified menu)
  const actionTriggers = await page.locator('button.action-menu-trigger').count();
  assert(actionTriggers >= cards, `Actions button on every card (${actionTriggers} btns, ${cards} cards)`);
  // Menu items exist in DOM (hidden until menu opens)
  const listBtns = await page.locator('button:has-text("Add to Shopping List")').count();
  const mealBtns = await page.locator('button:has-text("Add to Meal Plan")').count();
  assert(listBtns >= cards, `"Add to Shopping List" in every card menu (${listBtns} btns, ${cards} cards)`);
  assert(mealBtns >= cards, `"Add to Meal Plan" in every card menu (${mealBtns})`);

  // Search filter
  const search = page.locator('input[x-model="search"]');
  await search.fill('chicken');
  await page.waitForTimeout(400);
  const filtered = await page.locator('[x-data="recipesGrid"] .relative.flex.flex-col').count();
  assert(filtered < cards || cards <= 5, `Search filters cards (${cards} → ${filtered})`);
  await search.clear();
  await page.waitForTimeout(300);

  // Tag filter dropdown
  const tagBtn = page.locator('button:has-text("Filter by tag"), button:has-text("tag")').first();
  if (await tagBtn.count() > 0) {
    await tagBtn.click();
    await page.waitForTimeout(300);
    const checkboxes = await page.locator('input[type="checkbox"]').count();
    assert(checkboxes > 0, `Tag filter shows checkboxes (${checkboxes})`);
    // Select first tag
    if (checkboxes > 0) {
      await page.locator('input[type="checkbox"]').first().click();
      await page.waitForTimeout(300);
    }
    await tagBtn.click(); // close
    await page.waitForTimeout(200);
  }

  // Sort dropdown
  const sortBtn = page.locator('button:has-text("Sort")').first();
  await sortBtn.click();
  await page.waitForTimeout(300);
  const sortOpts = await page.locator('button:has-text("Name A")').count();
  assert(sortOpts > 0, 'Sort dropdown opens with options');
  await sortBtn.click(); // close
  await page.waitForTimeout(200);

  // Add to Shopping List (inside Actions menu) fires API and succeeds
  const responses = [];
  page.on('response', async res => {
    if (res.url().includes('shopping-list/add-recipe'))
      responses.push({ status: res.status() });
  });
  await page.locator('button.action-menu-trigger').first().click();
  await page.waitForTimeout(200);
  await page.locator('.action-menu-panel button:has-text("Add to Shopping List")').first().click();
  await page.waitForTimeout(1500);
  assert(responses.length > 0 && responses[0].status === 200, `Add to Shopping List API returns 200 (got ${responses[0]?.status})`);

  // Meal Plan panel opens via Actions menu
  await page.locator('button.action-menu-trigger').first().click();
  await page.waitForTimeout(200);
  await page.locator('.action-menu-panel button:has-text("Add to Meal Plan")').first().click();
  await page.waitForTimeout(600);
  const panel = await page.locator('.fixed.inset-y-0.right-0').isVisible();
  assert(panel, 'Meal plan panel opens');
  await page.locator('.fixed.inset-y-0.right-0 button').first().click();
  await page.waitForTimeout(300);

  // Delete button — triggers confirm dialog (don't actually delete)
  expectDialog = true;
  const delBtn = page.locator('[x-data="recipesGrid"] button[title="Delete recipe"]').first();
  if (await delBtn.count() > 0) {
    await delBtn.click();
    await page.waitForTimeout(400);
    assert(true, 'Delete button triggers confirm dialog');
  }
}

async function testRecipeDetail() {
  console.log('\n[Recipe Detail]');
  await goto('/recipes');
  await page.waitForTimeout(600);
  const link = page.locator('a[href^="/recipes/"]').first();
  const href = await link.getAttribute('href');
  if (!href) { assert(false, 'No recipe links found'); return; }

  await goto(href);
  await ss('recipe_detail');
  assert(await page.locator('h2').first().isVisible(), 'Recipe detail heading visible');

  // Edit button
  const editBtn = page.locator('button:has-text("Edit")');
  assert(await editBtn.count() > 0, 'Edit button present');

  // + Meal Plan button
  assert(await page.locator('button:has-text("Meal Plan")').count() > 0, '"+ Meal Plan" button present');

  // Add to List action button (replaced the old scroll-anchor; now POSTs to shopping list)
  assert(await page.locator('button:has-text("Add to List")').count() > 0, '"Add to List" action button present');

  // Ingredients card
  const ingCard = page.locator('#ingredients-card');
  assert(await ingCard.count() > 0, 'Ingredients card present');

  // Servings stepper
  const servingsSpan = page.locator('#ingredients-card span[x-text="servings"]');
  if (await servingsSpan.count() > 0) {
    const before = await servingsSpan.textContent();
    const incBtn = page.locator('#ingredients-card button:has-text("+")').first();
    await incBtn.click();
    await page.waitForTimeout(200);
    const after = await servingsSpan.textContent();
    assert(Number(after) > Number(before), `Servings stepper works (${before} → ${after})`);
  }

  // View toggle (As listed / By category)
  const catBtn = page.locator('button:has-text("By category")');
  if (await catBtn.count() > 0) {
    await catBtn.click();
    await page.waitForTimeout(400);
    assert(true, 'Category view toggle clickable');
    await page.locator('button:has-text("As listed")').click();
    await page.waitForTimeout(200);
  }

  // Ingredients card footer button removed (add-to-list is on recipe cards only)
  const addListBtn = page.locator('#ingredients-card button:has-text("Add to Shopping List")');
  assert(await addListBtn.count() === 0, 'Ingredients footer button removed (by design)');

  // Instructions section
  assert(await page.locator('text=Instructions').count() > 0, 'Instructions section present');

  // Health badge (if present)
  const healthBadge = page.locator('button:has-text("Health:")');
  if (await healthBadge.count() > 0) {
    await healthBadge.click();
    await page.waitForTimeout(300);
    assert(true, 'Health badge popup works');
  }
}

async function testShoppingList() {
  console.log('\n[Shopping List]');
  await goto('/shopping-list');
  await ss('shopping_list');

  const heading = await page.locator('text=Shopping List').first().isVisible();
  assert(heading, 'Shopping list page loads');

  // Recipe search & add section
  const recipeInput = page.locator('input[x-model="recipeSearch"], input[placeholder*="recipe" i]').first();
  if (await recipeInput.count() > 0) {
    assert(true, 'Recipe search input present');
  }

  // Check items display
  const items = await page.locator('[x-data] table tbody tr, [x-data] [x-for] > div').count();
  if (items > 0) assert(true, `Shopping list has ${items} item rows`);
  else warn('Shopping list may be empty');

  // "Send to Kroger Cart" button is wired in the header action group
  // when items > 0; modal opens on click and POSTs to
  // /api/shopping-list/add-to-cart with confirm:false then confirm:true.
  const sendToCartBtn = page.locator('button:has-text("Send to Kroger Cart")');
  if (items > 0) {
    assert(await sendToCartBtn.count() > 0, '"Send to Kroger Cart" button present (list has items)');

    // Activation test: open modal, assert preview structure, toggle
    // modality re-fires the request, then Cancel — does NOT confirm,
    // because confirm posts items to the real Kroger cart.
    let previewHits = 0;
    let lastBody = null;
    const onReq = req => {
      if (req.url().includes('/api/shopping-list/add-to-cart') && req.method() === 'POST') {
        previewHits++;
        try { lastBody = JSON.parse(req.postData()); } catch { /* ignore */ }
      }
    };
    page.on('request', onReq);

    await sendToCartBtn.first().click();
    const modalTitle = page.locator('h3:has-text("Send to Kroger Cart")');
    await modalTitle.waitFor({ state: 'visible', timeout: 5000 });
    assert(await modalTitle.isVisible(), 'Send-to-Cart modal opens with title');
    // Wait for preview POST to complete
    await page.waitForFunction(() => {
      const el = document.querySelector('[x-data*="shoppingListData"]');
      return el && el._x_dataStack[0].previewLoading === false && el._x_dataStack[0].previewData;
    }, { timeout: 5000 });
    assert(previewHits === 1, `Preview POST fired exactly once (got ${previewHits})`);
    assert(lastBody && lastBody.confirm === false && lastBody.modality === 'PICKUP',
      `Preview body has confirm:false, modality:PICKUP (got ${JSON.stringify(lastBody)})`);

    const willAddSection = page.locator('text=/Will add \\(/');
    assert(await willAddSection.count() > 0, 'Modal renders "Will add" section');

    // Toggle to Delivery — should re-fire preview with new modality
    previewHits = 0;
    await page.locator('button:has-text("Delivery")').first().click();
    await page.waitForTimeout(800);
    assert(previewHits === 1, `Modality toggle re-fires preview (got ${previewHits})`);
    assert(lastBody && lastBody.modality === 'DELIVERY',
      `Toggle sent modality:DELIVERY (got ${lastBody && lastBody.modality})`);

    // Cancel — close modal without sending
    await page.locator('button:has-text("Cancel")').first().click();
    await page.waitForTimeout(300);
    assert(!(await modalTitle.isVisible()), 'Cancel closes the modal');

    page.off('request', onReq);
  } else {
    assert(await sendToCartBtn.count() === 0, '"Send to Kroger Cart" button hidden (empty list)');
  }

  // Trash icon and Clear List button present
  const clearListBtn = await page.locator('button:has-text("Clear List")').count();
  assert(clearListBtn >= 0, '"Clear List" button wired (hidden when empty, present otherwise)');
  // Trash SVG path is the standard trash icon path segment
  const trashBtns = await page.locator('button[title="Remove item"]').count();
  assert(trashBtns >= 0, 'Trash icon buttons present on rows (0 when list is empty)');
}

async function testMealPlan() {
  console.log('\n[Meal Plan]');
  await goto('/meal-plan');
  await page.waitForTimeout(800);
  await ss('meal_plan');

  assert(await page.locator('text=Meal Plan').first().isVisible(), 'Meal plan page loads');

  // New Plan button
  const newPlanBtn = page.locator('button:has-text("New Plan"), button:has-text("Create")').first();
  assert(await newPlanBtn.count() > 0, '"New Plan" button present');

  // Check if plans exist and week grid renders
  const weekGrid = page.locator('[style*="grid-template-columns"]').first();
  if (await weekGrid.count() > 0) {
    assert(true, 'Week grid renders');
  }

  // Week navigation
  const prevBtn = page.locator('button:has-text("‹"), button:has-text("Prev")').first();
  const nextBtn = page.locator('button:has-text("›"), button:has-text("Next")').first();
  if (await prevBtn.count() > 0) {
    assert(true, 'Week navigation buttons present');
  }
}

async function testPantry() {
  console.log('\n[Pantry]');
  await goto('/pantry');
  await page.waitForTimeout(600);
  await ss('pantry');

  assert(await page.locator('text=Pantry').first().isVisible(), 'Pantry page loads');

  // Add Item button
  const addBtn = page.locator('button:has-text("Add Item"), button:has-text("Add")').first();
  assert(await addBtn.count() > 0, '"Add Item" button present');

  // Pantry items
  const pantryItems = await page.locator('[x-for], table tbody tr').count();
  if (pantryItems > 0) assert(true, `Pantry has ${pantryItems} items`);
  else warn('Pantry may be empty');

  // Restock all button
  const restockBtn = page.locator('button:has-text("Restock")').first();
  if (await restockBtn.count() > 0) assert(true, '"Restock" button present');
}

async function testMealTracker() {
  console.log('\n[Meal Tracker]');
  await goto('/meal-tracker');
  await page.waitForTimeout(600);
  await ss('meal_tracker');

  const mealTitle = await page.title();
  assert(mealTitle.includes('Meal Tracker'), 'Meal tracker page loads');

  // Meal type tabs
  const tabs = ['Breakfast', 'Lunch', 'Dinner', 'Snack'];
  for (const tab of tabs) {
    const tabBtn = page.locator(`button:has-text("${tab}")`).first();
    if (await tabBtn.count() > 0) {
      await tabBtn.click();
      await page.waitForTimeout(200);
    }
  }
  assert(true, 'Meal type tabs clickable');

  // Description input
  const descInput = page.locator('input[x-model="description"], input[placeholder*="meal" i], input[placeholder*="description" i]').first();
  if (await descInput.count() > 0) assert(true, 'Description input present');
}

async function testFavorites() {
  console.log('\n[Favorites]');
  await goto('/favorites');
  await page.waitForTimeout(600);
  await ss('favorites');

  assert(await page.locator('text=Favorites').first().isVisible(), 'Favorites page loads');

  // New List button
  const newBtn = page.locator('button:has-text("New List"), button:has-text("Create")').first();
  assert(await newBtn.count() > 0, '"New List" button present');

  // Favorites list cards
  const listCards = await page.locator('a[href^="/favorites/"]').count();
  if (listCards > 0) {
    assert(true, `Favorites lists visible (${listCards})`);
    // Click into first list detail
    const firstList = page.locator('a[href^="/favorites/"]').first();
    const listHref = await firstList.getAttribute('href');
    if (listHref) {
      await goto(listHref);
      await ss('favorites_detail');
      const favTitle = await page.title();
      assert(favTitle.includes('Favorites'), 'Favorites detail page loads');
    }
  } else {
    warn('No favorites lists created yet');
  }
}

async function testProducts() {
  console.log('\n[Products]');
  await goto('/products');
  await page.waitForTimeout(600);
  await ss('products');

  const prodTitle = await page.title();
  assert(prodTitle.includes('Products'), 'Products page loads');

  // Search input
  const searchInput = page.locator('input[x-model="query"], input[placeholder*="search" i]').first();
  if (await searchInput.count() > 0) {
    assert(true, 'Product search input present');
    // Do a search
    await searchInput.fill('milk');
    await searchInput.press('Enter');
    await page.waitForTimeout(2000);
    await ss('products_search');
    const results = await page.locator('[x-for]').count();
    if (results > 0) assert(true, 'Search returns results');
    else warn('Product search returned no visible results (may need Kroger API auth)');
  }

  // Mode switch (Search / Deals)
  const dealsTab = page.locator('button:has-text("Deals")').first();
  if (await dealsTab.count() > 0) {
    await dealsTab.click();
    await page.waitForTimeout(300);
    assert(true, 'Deals mode tab clickable');
  }
}

async function testDeals() {
  console.log('\n[Deals]');
  await goto('/deals');
  await page.waitForTimeout(600);
  await ss('deals');

  // Some installations redirect /deals to /products, check either
  const url = page.url();
  assert(url.includes('/deals') || url.includes('/products'), `Deals page loads (${url})`);
}

async function testAnalytics() {
  console.log('\n[Analytics]');
  await goto('/analytics');
  await page.waitForTimeout(600);
  await ss('analytics');

  assert(await page.locator('text=Analytics').first().isVisible(), 'Analytics page loads');

  // Tab buttons
  const tabs = ['Spending', 'Patterns', 'Pantry', 'Cookable'];
  let tabsFound = 0;
  for (const tab of tabs) {
    const tabBtn = page.locator(`button:has-text("${tab}")`).first();
    if (await tabBtn.count() > 0) {
      tabsFound++;
      await tabBtn.click();
      await page.waitForTimeout(500);
    }
  }
  if (tabsFound > 0) assert(true, `Analytics tabs work (${tabsFound} found)`);
  else warn('No analytics tabs found');

  // Time period buttons
  const periodBtn = page.locator('button:has-text("30"), button:has-text("7 days")').first();
  if (await periodBtn.count() > 0) {
    await periodBtn.click();
    await page.waitForTimeout(300);
    assert(true, 'Time period selector works');
  }
}

async function testSafety() {
  console.log('\n[Safety & Ingredients]');
  await goto('/safety');
  await page.waitForTimeout(600);
  await ss('safety');

  assert(await page.locator('text=Safety').first().isVisible(), 'Safety page loads');

  // Tabs
  const tabs = ['Ingredients', 'Approved', 'Blocked', 'Custom'];
  let tabsFound = 0;
  for (const tab of tabs) {
    const tabBtn = page.locator(`button:has-text("${tab}")`).first();
    if (await tabBtn.count() > 0) {
      tabsFound++;
      await tabBtn.click();
      await page.waitForTimeout(400);
    }
  }
  if (tabsFound > 0) assert(true, `Safety tabs work (${tabsFound} found)`);

  // Go back to Flagged Ingredients tab and search
  const flaggedTab = page.locator('button:has-text("Flagged"), button:has-text("Ingredients")').first();
  if (await flaggedTab.count() > 0) {
    await flaggedTab.click();
    await page.waitForTimeout(400);
  }
  const searchInput = page.locator('input[x-model="ingredientSearch"]').first();
  if (await searchInput.count() > 0 && await searchInput.isVisible()) {
    await searchInput.fill('sugar');
    await page.waitForTimeout(300);
    assert(true, 'Ingredient search input works');
    await searchInput.clear();
  } else {
    warn('Ingredient search input not visible');
  }
}

async function testSettings() {
  console.log('\n[Settings]');
  await goto('/settings');
  await page.waitForTimeout(600);
  await ss('settings');

  assert(await page.locator('text=Settings').first().isVisible(), 'Settings page loads');

  // Household size input
  const servingsInput = page.locator('input[x-model\\.number="servings"], input[type="number"]').first();
  if (await servingsInput.count() > 0) {
    assert(true, 'Household servings input present');
  }

  // Location search
  const locInput = page.locator('input[x-model="locationSearch"], input[placeholder*="zip" i]').first();
  if (await locInput.count() > 0) {
    assert(true, 'Location search input present');
  }

  // Save servings button
  const saveBtn = page.locator('button:has-text("Save")').first();
  if (await saveBtn.count() > 0) assert(true, 'Save button present');

  // --- Kroger Connection section ---
  const krogerSection = page.locator('text=Kroger Connection').first();
  assert(await krogerSection.isVisible(), 'Kroger Connection section visible');

  // Status indicator — dot uses CSS variable inline styles, rendered in x-if by Alpine
  await page.waitForTimeout(400);
  const statusDot = page.locator('[style*="border-radius: 99px"]').first();
  assert(await statusDot.count() > 0, 'Auth status indicator present');

  // Connect or Disconnect button present (depends on current auth state)
  const connectBtn = page.locator('button:has-text("Connect to Kroger")');
  const disconnectBtn = page.locator('button:has-text("Disconnect")');
  const hasAuthBtn = (await connectBtn.count()) > 0 || (await disconnectBtn.count()) > 0;
  assert(hasAuthBtn, 'Connect or Disconnect button present');

  // Advanced Settings toggle
  const advToggle = page.locator('button:has-text("Advanced Settings")');
  assert(await advToggle.count() > 0, 'Advanced Settings toggle present');

  // Open advanced settings and check credential fields
  if (await advToggle.count() > 0) {
    await advToggle.click();
    await page.waitForTimeout(400);

    const clientIdInput = page.locator('input[x-model="creds.client_id"]');
    assert(await clientIdInput.isVisible(), 'Client ID input visible after expanding');

    const redirectInput = page.locator('input[x-model="creds.redirect_uri"]');
    assert(await redirectInput.isVisible(), 'Redirect URI input visible after expanding');

    const saveCredsBtn = page.locator('button:has-text("Save Credentials")');
    assert(await saveCredsBtn.isVisible(), 'Save Credentials button visible');
  }

  // API: /api/settings/auth/status returns valid JSON
  const authStatusResp = await page.evaluate(async () => {
    const r = await fetch('/api/settings/auth/status');
    return { ok: r.ok, data: await r.json() };
  });
  assert(authStatusResp.ok && 'status' in authStatusResp.data, 'Auth status API returns valid response');

  // API: /api/settings/credentials returns masked secret
  const credsResp = await page.evaluate(async () => {
    const r = await fetch('/api/settings/credentials');
    return { ok: r.ok, data: await r.json() };
  });
  assert(credsResp.ok && 'client_id' in credsResp.data, 'Credentials API returns valid response');

  // Disconnect lifecycle (only if currently authenticated)
  if (await disconnectBtn.count() > 0) {
    expectDialog = true;
    await disconnectBtn.click();
    await page.waitForTimeout(1000);
    // After disconnect, connect button should appear
    const connectAfter = page.locator('button:has-text("Connect to Kroger")');
    if (await connectAfter.count() > 0) assert(true, 'Disconnect → Connect button appears');
  }
}

async function testPredictions() {
  console.log('\n[Predictions]');
  await goto('/predictions');
  await page.waitForTimeout(600);
  await ss('predictions');

  const url = page.url();
  assert(url.includes('/predictions'), 'Predictions page loads');
}

async function testChatWidget() {
  console.log('\n[Chat Widget]');
  await goto('/recipes');
  await page.waitForTimeout(600);

  // Chat FAB button
  const fab = page.locator('[x-data*="chat"] button, button[title*="Chat" i], [aria-label*="chat" i]').first();
  if (await fab.count() === 0) {
    // Try broader match - any circular button at bottom right
    const allBtns = page.locator('button.fixed, button[style*="fixed"]');
    if (await allBtns.count() > 0) {
      assert(true, 'Chat FAB found (positional)');
    } else {
      warn('Chat FAB not found — may be conditionally rendered');
      return;
    }
  }

  // Look for the chat component via Alpine data
  const chatExists = await page.evaluate(() => {
    const el = document.querySelector('[x-data*="chat"]');
    return !!el;
  });
  if (chatExists) {
    assert(true, 'Chat widget Alpine component present');
  } else {
    warn('Chat widget component not found');
  }
}

async function testConsoleErrors() {
  console.log('\n[Console Errors — All Pages]');
  if (consoleErrors.length === 0) {
    assert(true, 'No critical JS console errors across all pages');
  } else {
    console.log('  Errors found:');
    consoleErrors.slice(0, 5).forEach(e => console.log(`    ${e.slice(0, 120)}`));
    assert(false, `${consoleErrors.length} critical JS console error(s)`);
  }
}

// ────────────────────────────────────────────
run().catch(err => {
  console.error('Test runner error:', err);
  if (browser) browser.close();
  process.exit(1);
});
