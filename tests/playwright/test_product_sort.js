// @ts-check
/**
 * Product Page Sort Feature — E2E Tests
 *
 * Tests the new favorites-first sort system:
 *   1. Sort UI appears after search results load
 *   2. Favorites sort pill is active by default
 *   3. Sort stack toggles work (add/remove)
 *   4. Clear button resets sort
 *   5. Preferences persist across page reloads
 *   6. Deals mode sort still works independently
 *   7. API endpoint saves and loads correctly
 *
 * Usage: node tests/playwright/test_product_sort.js
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8080';
const SCREENSHOT_DIR = path.join(__dirname, 'screenshots');
const PAUSE_MS = 600;
const SHORT_PAUSE = 300;
const LONG_PAUSE = 1500;

if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

let browser, page;
let passed = 0, failed = 0, warnings = 0;
const consoleErrors = [];
const testResults = [];

function assert(condition, label) {
  if (condition) {
    console.log(`  \x1b[32mPASS\x1b[0m: ${label}`);
    passed++;
    testResults.push({ status: 'PASS', label });
  } else {
    console.log(`  \x1b[31mFAIL\x1b[0m: ${label}`);
    failed++;
    testResults.push({ status: 'FAIL', label });
  }
}

function warn(label) {
  console.log(`  \x1b[33mWARN\x1b[0m: ${label}`);
  warnings++;
  testResults.push({ status: 'WARN', label });
}

async function ss(name) {
  const p = path.join(SCREENSHOT_DIR, `sort_${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function pause(ms = PAUSE_MS) {
  await page.waitForTimeout(ms);
}

async function setup() {
  browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await ctx.newPage();

  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', err => consoleErrors.push(err.message));

  console.log('\n========================================');
  console.log('Product Sort Feature — E2E Tests');
  console.log('========================================\n');
}

async function teardown() {
  const report = {
    timestamp: new Date().toISOString(),
    summary: { passed, failed, warnings, total: passed + failed },
    results: testResults,
    consoleErrors,
  };
  fs.writeFileSync(
    path.join(SCREENSHOT_DIR, '..', 'test_sort_report.json'),
    JSON.stringify(report, null, 2)
  );
  await browser.close();

  console.log('\n========================================');
  console.log(`Results: \x1b[32m${passed} passed\x1b[0m, \x1b[31m${failed} failed\x1b[0m, \x1b[33m${warnings} warnings\x1b[0m`);
  console.log('========================================\n');
  process.exit(failed > 0 ? 1 : 0);
}

// ── Helpers ─────────────────────────────────────────────────────────────────

/** Reset sort preferences to default via the API so each test starts clean. */
async function resetSortPrefs(searchStack = ['favorites'], dealsStack = []) {
  const resp = await page.request.post(`${BASE}/api/settings/product-sort`, {
    data: { search_sort_stack: searchStack, deals_sort_stack: dealsStack },
  });
  assert(resp.ok(), `Reset sort prefs via API (status ${resp.status()})`);
}

/** Search for 'milk' and wait for either results or API-unavailable message. */
async function doSearch(term = 'milk') {
  const input = page.locator('input[placeholder*="Search" i], input[placeholder*="search" i]').first();
  await input.fill(term);
  await input.press('Enter');
  // Wait for loading spinner to disappear or results to appear
  await page.waitForTimeout(LONG_PAUSE);
}

// ═══════════════════════════════════════════════════════════════════════════
// SUITE 1 — API Endpoint
// ═══════════════════════════════════════════════════════════════════════════
async function testApiEndpoint() {
  console.log('\n─── SUITE 1: API Endpoint ───');

  await page.goto(`${BASE}/products`);
  await page.waitForLoadState('networkidle');

  // GET returns default or saved prefs
  const getResp = await page.request.get(`${BASE}/api/settings/product-sort`);
  assert(getResp.ok(), `GET /api/settings/product-sort returns 200 (got ${getResp.status()})`);

  const prefs = await getResp.json();
  assert(Array.isArray(prefs.search_sort_stack), 'Response has search_sort_stack array');
  assert(Array.isArray(prefs.deals_sort_stack), 'Response has deals_sort_stack array');

  // POST saves preferences
  const postResp = await page.request.post(`${BASE}/api/settings/product-sort`, {
    data: { search_sort_stack: ['favorites', 'health'], deals_sort_stack: ['percent'] },
  });
  assert(postResp.ok(), `POST /api/settings/product-sort returns 200 (got ${postResp.status()})`);
  const postBody = await postResp.json();
  assert(postBody.success === true, 'POST response has success: true');

  // GET now returns saved values
  const getResp2 = await page.request.get(`${BASE}/api/settings/product-sort`);
  const prefs2 = await getResp2.json();
  assert(
    JSON.stringify(prefs2.search_sort_stack) === JSON.stringify(['favorites', 'health']),
    `GET returns saved search stack: ${JSON.stringify(prefs2.search_sort_stack)}`
  );
  assert(
    JSON.stringify(prefs2.deals_sort_stack) === JSON.stringify(['percent']),
    `GET returns saved deals stack: ${JSON.stringify(prefs2.deals_sort_stack)}`
  );

  // Validation: invalid keys get stripped
  const badResp = await page.request.post(`${BASE}/api/settings/product-sort`, {
    data: { search_sort_stack: ['favorites', 'INJECTED_KEY', 'price'], deals_sort_stack: [] },
  });
  assert(badResp.ok(), 'POST with invalid keys returns 200');
  const saved = await page.request.get(`${BASE}/api/settings/product-sort`);
  const savedPrefs = await saved.json();
  assert(
    !savedPrefs.search_sort_stack.includes('INJECTED_KEY'),
    'Invalid key "INJECTED_KEY" was stripped by validation'
  );
  assert(
    savedPrefs.search_sort_stack.includes('favorites') &&
    savedPrefs.search_sort_stack.includes('price'),
    'Valid keys survived validation'
  );

  // Reset for next suites
  await resetSortPrefs(['favorites'], []);
}

// ═══════════════════════════════════════════════════════════════════════════
// SUITE 2 — Page Load: Preferences Hydration
// ═══════════════════════════════════════════════════════════════════════════
async function testPrefsHydration() {
  console.log('\n─── SUITE 2: Page Load / Preference Hydration ───');

  // Set known prefs before loading
  await resetSortPrefs(['favorites', 'health'], ['percent']);

  await page.goto(`${BASE}/products`);
  await page.waitForLoadState('networkidle');
  await pause();
  await ss('02_page_loaded');

  // Verify Alpine.js received the server-injected values
  const searchStack = await page.evaluate(() => {
    const el = document.querySelector('[x-data]');
    return el ? el._x_dataStack?.[0]?.searchSortStack ?? null : null;
  });
  if (searchStack !== null) {
    assert(
      Array.isArray(searchStack) && searchStack.includes('favorites'),
      `searchSortStack hydrated from server: ${JSON.stringify(searchStack)}`
    );
  } else {
    // Alpine may store data differently — check via JS eval
    warn('Could not directly read Alpine data (structure may differ)');
  }

  // Sort pills bar is NOT visible before any search (no results yet)
  const sortBar = page.locator('text="Sort:"').first();
  const sortBarVisible = await sortBar.isVisible().catch(() => false);
  // The sort bar for search mode should only appear when products.length > 0
  assert(!sortBarVisible, 'Sort pills bar hidden before search results appear');

  // Reset to clean defaults
  await resetSortPrefs(['favorites'], []);
}

// ═══════════════════════════════════════════════════════════════════════════
// SUITE 3 — Search Sort UI
// ═══════════════════════════════════════════════════════════════════════════
async function testSearchSortUI() {
  console.log('\n─── SUITE 3: Search Sort UI ───');

  await resetSortPrefs(['favorites'], []);
  await page.goto(`${BASE}/products`);
  await page.waitForLoadState('networkidle');

  // Trigger a search
  await doSearch('milk');
  await ss('03_after_search');

  // Check if we got results vs. API unavailable
  const resultCards = await page.locator('.grid.grid-cols-3 > div').count();
  const hasResults = resultCards > 0;

  if (!hasResults) {
    warn('No search results returned (Kroger API may be unavailable) — testing UI structure only');

    // Sort bar should NOT be visible with no results (x-show hides it, but it stays in DOM)
    const sortBarVisible = await page.locator('div:has(> span:text-is("Sort:"))').first().isVisible().catch(() => false);
    assert(!sortBarVisible, 'Sort bar correctly hidden when no results');
    return;
  }

  console.log(`    Found ${resultCards} result cards`);

  // Sort bar SHOULD appear now
  await page.waitForSelector('text="Sort:"', { timeout: 3000 }).catch(() => null);
  const sortBarVisible = await page.locator('text="Sort:"').first().isVisible().catch(() => false);
  assert(sortBarVisible, 'Sort pills bar appears after search results load');

  await ss('03_sort_bar_visible');

  // Favorites pill should be present and active (default)
  const favPill = page.locator('button:has-text("Favorites")').first();
  const favPillVisible = await favPill.isVisible().catch(() => false);
  assert(favPillVisible, 'Favorites sort pill is visible');

  if (favPillVisible) {
    // Should be active (amber background) by default
    const favClass = await favPill.getAttribute('class');
    const isActive = favClass && (favClass.includes('bg-amber-500') || favClass.includes('bg-amber'));
    assert(isActive, `Favorites pill active by default (classes: ${favClass?.substring(0, 60)}...)`);

    // Priority number "1." should show
    const prioritySpan = favPill.locator('span').filter({ hasText: /^1\. $/ });
    const hasPriority = await prioritySpan.isVisible().catch(() => false);
    assert(hasPriority, 'Favorites pill shows priority "1." when active');
  }

  // Healthiest pill present
  const healthPill = page.locator('button:has-text("Healthiest")').first();
  assert(await healthPill.isVisible().catch(() => false), 'Healthiest sort pill visible');

  // Price pill present
  const pricePill = page.locator('button:has-text("Price ↑")').first();
  assert(await pricePill.isVisible().catch(() => false), 'Price sort pill visible');

  // Clear button should appear (favorites is active)
  const clearBtn = page.locator('button:has-text("Clear")').first();
  const clearVisible = await clearBtn.isVisible().catch(() => false);
  assert(clearVisible, 'Clear button visible when sort stack is non-empty');
}

// ═══════════════════════════════════════════════════════════════════════════
// SUITE 4 — Sort Toggle Interactions
// ═══════════════════════════════════════════════════════════════════════════
async function testSortToggleInteractions() {
  console.log('\n─── SUITE 4: Sort Toggle Interactions ───');

  await resetSortPrefs(['favorites'], []);
  await page.goto(`${BASE}/products`);
  await page.waitForLoadState('networkidle');
  await doSearch('milk');
  await pause();

  const resultCards = await page.locator('.grid.grid-cols-3 > div').count();
  if (!resultCards) {
    warn('No results — skipping interaction tests');
    return;
  }

  // --- Toggle OFF favorites ---
  const favPill = page.locator('button:has-text("Favorites")').first();
  if (await favPill.isVisible().catch(() => false)) {
    await favPill.click();
    await pause(SHORT_PAUSE);
    await ss('04_favorites_off');

    const favClassAfter = await favPill.getAttribute('class');
    const isInactive = favClassAfter && !favClassAfter.includes('bg-amber-500');
    assert(isInactive, 'Clicking Favorites pill deactivates it');

    // Clear button should hide when stack is empty
    const clearVisible = await page.locator('button:has-text("Clear")').first().isVisible().catch(() => false);
    assert(!clearVisible, 'Clear button hidden when sort stack is empty');
  }

  // --- Toggle ON health sort ---
  const healthPill = page.locator('button:has-text("Healthiest")').first();
  if (await healthPill.isVisible().catch(() => false)) {
    await healthPill.click();
    await pause(SHORT_PAUSE);
    await ss('04_health_active');

    const healthClass = await healthPill.getAttribute('class');
    const isActive = healthClass && healthClass.includes('bg-emerald-600');
    assert(isActive, 'Healthiest pill becomes active when clicked');

    // Priority "1." shows
    const priorityText = await healthPill.textContent();
    assert(priorityText?.includes('1.'), `Health pill shows priority 1 (text: "${priorityText?.trim()}")`);
  }

  // --- Toggle ON price sort (multi-sort) ---
  const pricePill = page.locator('button:has-text("Price ↑")').first();
  if (await pricePill.isVisible().catch(() => false)) {
    await pricePill.click();
    await pause(SHORT_PAUSE);
    await ss('04_health_and_price');

    const healthPillText = await healthPill.textContent().catch(() => '');
    const pricePillText = await pricePill.textContent().catch(() => '');
    assert(healthPillText?.includes('1.'), 'Health still shows as priority 1');
    assert(pricePillText?.includes('2.'), `Price shows as priority 2 (text: "${pricePillText?.trim()}")`);
  }

  // --- Clear all ---
  const clearBtn = page.locator('button:has-text("Clear")').first();
  if (await clearBtn.isVisible().catch(() => false)) {
    await clearBtn.click();
    await pause(SHORT_PAUSE);
    await ss('04_cleared');

    const clearVisible = await clearBtn.isVisible().catch(() => false);
    assert(!clearVisible, 'Clear button disappears after clearing');

    const healthClassAfterClear = await healthPill.getAttribute('class').catch(() => '');
    const healthInactive = !healthClassAfterClear?.includes('bg-emerald-600');
    assert(healthInactive, 'Health pill deactivated after clear');
  }

  // Check no JS errors from all the toggling
  const errors = consoleErrors.filter(e => !e.includes('favicon'));
  assert(errors.length === 0, `No JS errors during sort toggle interactions (${errors.length} found)`);
  consoleErrors.length = 0;
}

// ═══════════════════════════════════════════════════════════════════════════
// SUITE 5 — Persistence (reload test)
// ═══════════════════════════════════════════════════════════════════════════
async function testPersistence() {
  console.log('\n─── SUITE 5: Persistence Across Reloads ───');

  // Set a non-default combination
  await resetSortPrefs(['health', 'price'], ['percent', 'health']);

  await page.goto(`${BASE}/products`);
  await page.waitForLoadState('networkidle');
  await pause();
  await ss('05_after_reload_with_prefs');

  // Verify the API still has our saved values
  const getResp = await page.request.get(`${BASE}/api/settings/product-sort`);
  const prefs = await getResp.json();
  assert(
    JSON.stringify(prefs.search_sort_stack) === JSON.stringify(['health', 'price']),
    `Search sort survived reload: ${JSON.stringify(prefs.search_sort_stack)}`
  );
  assert(
    JSON.stringify(prefs.deals_sort_stack) === JSON.stringify(['percent', 'health']),
    `Deals sort survived reload: ${JSON.stringify(prefs.deals_sort_stack)}`
  );

  // Now trigger a search to see the pills reflect persisted state
  await doSearch('milk');
  await pause();

  const resultCards = await page.locator('.grid.grid-cols-3 > div').count();
  if (resultCards > 0) {
    await ss('05_search_with_persisted_sort');

    const healthPill = page.locator('button:has-text("Healthiest")').first();
    if (await healthPill.isVisible().catch(() => false)) {
      const healthClass = await healthPill.getAttribute('class');
      const isActive = healthClass?.includes('bg-emerald-600');
      assert(isActive, 'Healthiest pill is active (restored from persisted prefs)');
    }

    const favPill = page.locator('button:has-text("Favorites")').first();
    if (await favPill.isVisible().catch(() => false)) {
      const favClass = await favPill.getAttribute('class');
      const isInactive = !favClass?.includes('bg-amber-500');
      assert(isInactive, 'Favorites pill is inactive (not in persisted prefs)');
    }
  } else {
    warn('No results for persistence UI check (API unavailable)');
  }

  // Test auto-save: change via UI and verify API updated
  await page.goto(`${BASE}/products`);
  await page.waitForLoadState('networkidle');
  await doSearch('milk');
  await pause();

  const resultCards2 = await page.locator('.grid.grid-cols-3 > div').count();
  if (resultCards2 > 0) {
    const favPill = page.locator('button:has-text("Favorites")').first();
    if (await favPill.isVisible().catch(() => false)) {
      await favPill.click();
      await pause(800); // Wait for debounced save (500ms)

      const getResp2 = await page.request.get(`${BASE}/api/settings/product-sort`);
      const prefs2 = await getResp2.json();
      assert(
        prefs2.search_sort_stack.includes('favorites'),
        `Auto-save: Favorites added to search stack after click (stack: ${JSON.stringify(prefs2.search_sort_stack)})`
      );
    }
  } else {
    warn('No results for auto-save test (API unavailable)');
  }

  // Reset to defaults
  await resetSortPrefs(['favorites'], []);
}

// ═══════════════════════════════════════════════════════════════════════════
// SUITE 6 — Deals Mode Independence
// ═══════════════════════════════════════════════════════════════════════════
async function testDealsModeIndependence() {
  console.log('\n─── SUITE 6: Deals Mode Independence ───');

  await resetSortPrefs(['favorites'], ['percent']);
  await page.goto(`${BASE}/products`);
  await page.waitForLoadState('networkidle');
  await pause(LONG_PAUSE);

  // Switch to deals mode
  const dealsTab = page.locator('button:has-text("Deals")').first();
  assert(await dealsTab.isVisible().catch(() => false), 'Deals tab button visible');

  if (await dealsTab.isVisible().catch(() => false)) {
    await dealsTab.click();
    await pause(LONG_PAUSE);
    await ss('06_deals_mode');

    // Deals sort bar (from existing code) should show when deals load
    // It might not show until deals are loaded
    const dealsGrid = await page.locator('.grid.grid-cols-3 > div').count();
    console.log(`    Deals loaded: ${dealsGrid} cards`);

    if (dealsGrid > 0) {
      await ss('06_deals_with_results');

      // The existing % Saved / $ Saved / Healthiest pills
      const percentPill = page.locator('button:has-text("% Saved")').first();
      assert(await percentPill.isVisible().catch(() => false), 'Deals: % Saved pill visible');

      // % Saved pill should be active (we set deals_sort_stack to ['percent'])
      if (await percentPill.isVisible().catch(() => false)) {
        const percentClass = await percentPill.getAttribute('class');
        const isActive = percentClass?.includes('bg-emerald-600');
        assert(isActive, 'Deals: % Saved pill active from persisted prefs');
      }

      // Favorites pill in deals mode should be separate
      const dealsFavPill = page.locator('button:has-text("Favorites")').first();
      const dealsFavVisible = await dealsFavPill.isVisible().catch(() => false);
      if (dealsFavVisible) {
        const dealsFavClass = await dealsFavPill.getAttribute('class');
        // Should NOT be active (our deals_sort_stack only has 'percent')
        const isInactive = !dealsFavClass?.includes('bg-amber-500');
        assert(isInactive, 'Deals: Favorites pill inactive (not in deals prefs)');
      }
    } else {
      warn('No deals loaded (Kroger API may be unavailable)');
    }

    // Switch back to search — search prefs should be independent
    const searchTab = page.locator('button:has-text("Search")').first();
    await searchTab.click();
    await doSearch('eggs');
    await pause();

    const searchCards = await page.locator('.grid.grid-cols-3 > div').count();
    if (searchCards > 0) {
      const favPill = page.locator('button:has-text("Favorites")').first();
      if (await favPill.isVisible().catch(() => false)) {
        const favClass = await favPill.getAttribute('class');
        const isActive = favClass?.includes('bg-amber-500');
        assert(isActive, 'Search: Favorites pill still active after deals mode interaction');
      }
    } else {
      warn('No search results for mode-independence check');
    }
  }

  // Reset to defaults
  await resetSortPrefs(['favorites'], []);
}

// ═══════════════════════════════════════════════════════════════════════════
// SUITE 7 — No Console Errors
// ═══════════════════════════════════════════════════════════════════════════
async function testNoConsoleErrors() {
  console.log('\n─── SUITE 7: No Console Errors ───');

  consoleErrors.length = 0;

  await page.goto(`${BASE}/products`);
  await page.waitForLoadState('networkidle');
  await pause(LONG_PAUSE);

  const loadErrors = consoleErrors.filter(e => !e.includes('favicon') && !e.includes('net::ERR'));
  assert(loadErrors.length === 0, `No JS errors on page load (found: ${loadErrors.join(', ') || 'none'})`);
  consoleErrors.length = 0;

  // Interact with sort prefs via API — check no errors
  await resetSortPrefs(['health', 'favorites'], []);
  await page.reload();
  await page.waitForLoadState('networkidle');
  await pause();

  const reloadErrors = consoleErrors.filter(e => !e.includes('favicon') && !e.includes('net::ERR'));
  assert(reloadErrors.length === 0, `No JS errors after preference reload (found: ${reloadErrors.join(', ') || 'none'})`);
  consoleErrors.length = 0;

  await resetSortPrefs(['favorites'], []);
}

// ═══════════════════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════════════════
async function main() {
  await setup();
  try {
    await testApiEndpoint();
    await testPrefsHydration();
    await testSearchSortUI();
    await testSortToggleInteractions();
    await testPersistence();
    await testDealsModeIndependence();
    await testNoConsoleErrors();
  } catch (err) {
    console.error('\n\x1b[31mFATAL ERROR:\x1b[0m', err.message);
    console.error(err.stack);
    failed++;
  } finally {
    await teardown();
  }
}

main();
