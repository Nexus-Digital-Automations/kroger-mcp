/**
 * Playwright tests for the unified action-menu component.
 *
 * OWNS: behavioral validation of action_menu.js / _macros/action_menu.html.
 * DOES NOT OWN: network-call correctness (covered by test_all_buttons.js and
 *   per-feature tests); static-file serving (covered by app.py tests).
 *
 * Page targets chosen for reliable server-rendered cards (no search needed):
 *   /recipes               — recipe cards (no submenus; good for root-level tests)
 *   /favorites/<list_id>   — favorite_item cards (have favorites + recipes submenus)
 *
 * CALLED BY: CI and spec validation. Run with: node tests/playwright/test_action_menu.js
 */
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8000';
// A favorites list with known items — provides favorite_item cards with submenus.
const FAV_LIST_PATH = '/favorites/weekly-essentials-4003a366';
const TIMEOUT = 10000;

let browser, passed = 0, failed = 0;
const failures = [];

function ok(name) { passed++; console.log(`  ✓ ${name}`); }
function fail(name, err) {
  failed++;
  failures.push({ test: name, error: err?.message || String(err) });
  console.log(`  ✗ ${name} — ${err?.message || err}`);
}
async function test(name, fn) {
  try { await fn(); ok(name); } catch (e) { fail(name, e); }
}

async function makeCtx(width = 1440, height = 900) {
  return browser.newContext({ viewport: { width, height } });
}

async function openPage(ctx, path) {
  const page = await ctx.newPage();
  page.setDefaultTimeout(TIMEOUT);
  await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(800);
  return page;
}

async function firstTrigger(page) {
  const btn = page.locator('button.action-menu-trigger').first();
  await btn.waitFor({ state: 'visible', timeout: 5000 });
  return btn;
}

// ── aria-expanded toggle ──────────────────────────────────────────────────────

async function testAriaExpandedToggle() {
  console.log('\n[aria-expanded toggle]');
  const ctx = await makeCtx();
  // /recipes: server-rendered cards always present without a search.
  const page = await openPage(ctx, '/recipes');

  await test('Actions trigger starts with aria-expanded="false"', async () => {
    const btn = await firstTrigger(page);
    const val = await btn.getAttribute('aria-expanded');
    if (val !== 'false') throw new Error(`expected "false", got "${val}"`);
  });

  await test('aria-expanded becomes "true" after click', async () => {
    const btn = await firstTrigger(page);
    await btn.click();
    await page.waitForTimeout(150);
    const val = await btn.getAttribute('aria-expanded');
    if (val !== 'true') throw new Error(`expected "true", got "${val}"`);
  });

  await test('aria-expanded returns to "false" after second click (toggle)', async () => {
    const btn = await firstTrigger(page);
    await btn.click();
    await page.waitForTimeout(150);
    const val = await btn.getAttribute('aria-expanded');
    if (val !== 'false') throw new Error(`expected "false", got "${val}"`);
  });

  await ctx.close();
}

// ── Keyboard traversal ───────────────────────────────────────────────────────

async function testKeyboardTraversal() {
  console.log('\n[keyboard traversal]');
  const ctx = await makeCtx();
  // /recipes: recipe cards have 4 root-level leaves (no submenus) — clean traversal target.
  const page = await openPage(ctx, '/recipes');

  await test('ArrowDown on focused trigger opens the menu', async () => {
    const btn = await firstTrigger(page);
    await btn.focus();
    await page.keyboard.press('ArrowDown');
    await page.waitForTimeout(150);
    const val = await btn.getAttribute('aria-expanded');
    if (val !== 'true') throw new Error(`menu did not open; aria-expanded="${val}"`);
  });

  await test('ArrowDown moves focus to a menuitem', async () => {
    await page.keyboard.press('ArrowDown');
    await page.waitForTimeout(100);
    const role = await page.evaluate(() => document.activeElement?.getAttribute('role'));
    if (role !== 'menuitem') throw new Error(`focus not on menuitem, role="${role}"`);
  });

  await test('ArrowUp cycles focus within visible menuitems', async () => {
    await page.keyboard.press('ArrowUp');
    await page.waitForTimeout(100);
    const role = await page.evaluate(() => document.activeElement?.getAttribute('role'));
    if (role !== 'menuitem') throw new Error(`focus lost after ArrowUp, role="${role}"`);
  });

  await test('Home moves focus to first menuitem', async () => {
    await page.keyboard.press('Home');
    await page.waitForTimeout(100);
    const isFirst = await page.evaluate(() => {
      const el = document.activeElement;
      const items = Array.from(
        document.querySelectorAll('[data-menu-level="root"] [role="menuitem"]')
      ).filter(e => e.offsetParent !== null && !e.disabled);
      return items.length > 0 && items[0] === el;
    });
    if (!isFirst) throw new Error('Home did not move focus to first menuitem');
  });

  await test('End moves focus to last menuitem', async () => {
    await page.keyboard.press('End');
    await page.waitForTimeout(100);
    const isLast = await page.evaluate(() => {
      const el = document.activeElement;
      const items = Array.from(
        document.querySelectorAll('[data-menu-level="root"] [role="menuitem"]')
      ).filter(e => e.offsetParent !== null && !e.disabled);
      return items.length > 0 && items[items.length - 1] === el;
    });
    if (!isLast) throw new Error('End did not move focus to last menuitem');
  });

  await test('Escape closes the menu', async () => {
    await page.keyboard.press('Escape');
    await page.waitForTimeout(150);
    const btn = await firstTrigger(page);
    const val = await btn.getAttribute('aria-expanded');
    if (val !== 'false') throw new Error(`menu still open after Escape, aria-expanded="${val}"`);
  });

  // Submenu keyboard tests need a card type that has submenus (favorite_item).
  const ctx2 = await makeCtx();
  const page2 = await openPage(ctx2, FAV_LIST_PATH);

  await test('ArrowRight on submenu trigger opens the submenu', async () => {
    const btn = await firstTrigger(page2);
    await btn.click();
    await page2.waitForTimeout(150);
    const sub = page2.locator('[data-submenu-trigger="favorites"]').first();
    await sub.focus();
    await page2.keyboard.press('ArrowRight');
    await page2.waitForTimeout(200);
    const expanded = await sub.getAttribute('aria-expanded');
    if (expanded !== 'true') throw new Error(`submenu did not open; aria-expanded="${expanded}"`);
  });

  await test('ArrowLeft from within submenu closes it', async () => {
    await page2.keyboard.press('ArrowLeft');
    await page2.waitForTimeout(150);
    const sub = page2.locator('[data-submenu-trigger="favorites"]').first();
    const expanded = await sub.getAttribute('aria-expanded');
    if (expanded !== 'false') throw new Error('submenu still open after ArrowLeft');
  });

  await ctx2.close();
}

// ── Focus return to trigger ───────────────────────────────────────────────────

async function testFocusReturn() {
  console.log('\n[focus return]');
  const ctx = await makeCtx();
  const page = await openPage(ctx, '/recipes');

  await test('Focus returns to the trigger after Escape closes the menu', async () => {
    const btn = await firstTrigger(page);
    await btn.click();
    await page.waitForTimeout(150);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    const hasFocus = await page.evaluate(() =>
      Array.from(document.querySelectorAll('button.action-menu-trigger'))
        .some(t => t === document.activeElement)
    );
    if (!hasFocus) throw new Error('focus did not return to any action-menu trigger');
  });

  await ctx.close();
}

// ── Mobile accordion back ─────────────────────────────────────────────────────

async function testMobileAccordionBack() {
  console.log('\n[mobile accordion back]');
  // favorite_item cards have a favorites submenu — required for accordion drill-down.
  const ctx = await makeCtx(375, 812);
  const page = await openPage(ctx, FAV_LIST_PATH);

  await test('Mobile: action-menu panel has data-mode="mobile"', async () => {
    const btn = await firstTrigger(page);
    await btn.click();
    await page.waitForTimeout(200);
    const mode = await page.locator('.action-menu-panel').first().getAttribute('data-mode');
    if (mode !== 'mobile') throw new Error(`expected "mobile", got "${mode}"`);
  });

  await test('Mobile: opening favorites submenu shows a Back button', async () => {
    const sub = page.locator('[data-submenu-trigger="favorites"]').first();
    await sub.waitFor({ state: 'visible', timeout: 3000 });
    await sub.click();
    await page.waitForTimeout(200);
    await page.locator('.action-menu-submenu .action-menu-back').first()
      .waitFor({ state: 'visible', timeout: 3000 });
  });

  await test('Mobile: tapping Back returns to parent level', async () => {
    await page.locator('.action-menu-submenu .action-menu-back').first().click();
    // Move mouse to neutral to prevent mouseenter on the now-revealed trigger.
    await page.mouse.move(0, 0);
    // waitFor retries until Alpine re-shows the root level (x-show is async).
    await page.locator('.action-menu-panel[data-mode="mobile"] [data-menu-level="root"]')
      .first().waitFor({ state: 'visible', timeout: 2000 });
    // Inline display:none from x-show beats the CSS display:flex rule.
    const subDisplay = await page.locator('[data-menu-level="favorites"]').first()
      .evaluate(el => getComputedStyle(el).display).catch(() => 'none');
    if (subDisplay !== 'none') throw new Error(`favorites submenu display="${subDisplay}" after Back tap`);
  });

  await ctx.close();
}

// ── Hover-intent delay ────────────────────────────────────────────────────────

async function testHoverIntentDelay() {
  console.log('\n[hover-intent delay]');
  // favorite_item cards have a favorites submenu to test hover-intent on.
  const ctx = await makeCtx(1440, 900);
  const page = await openPage(ctx, FAV_LIST_PATH);

  await test('Submenu stays open within the 180ms close window', async () => {
    const btn = await firstTrigger(page);
    await btn.click();
    await page.waitForTimeout(150);
    const sub = page.locator('[data-submenu-trigger="favorites"]').first();
    await sub.hover();
    await page.waitForTimeout(150);
    // Move mouse away — starts the 180ms scheduleClose timer.
    await page.mouse.move(0, 0);
    await page.waitForTimeout(100); // 100ms < 180ms: timer not yet fired
    const expanded = await sub.getAttribute('aria-expanded');
    if (expanded !== 'true') throw new Error('submenu closed too early (before 180ms)');
  });

  await test('Submenu closes after the 180ms hover-intent window expires', async () => {
    await page.waitForTimeout(200); // push past the 180ms timer
    const sub = page.locator('[data-submenu-trigger="favorites"]').first();
    const expanded = await sub.getAttribute('aria-expanded');
    if (expanded !== 'false') throw new Error('submenu still open after 180ms window expired');
  });

  await ctx.close();
}

// ── Submenu ARIA attributes ───────────────────────────────────────────────────

async function testSubmenuAriaAttributes() {
  console.log('\n[submenu ARIA attributes]');
  const ctx = await makeCtx(1440, 900);
  const page = await openPage(ctx, FAV_LIST_PATH);

  await test('Submenu triggers have aria-haspopup="menu"', async () => {
    const btn = await firstTrigger(page);
    await btn.click();
    await page.waitForTimeout(150);
    const sub = page.locator('[data-submenu-trigger]').first();
    await sub.waitFor({ state: 'visible', timeout: 3000 });
    const hp = await sub.getAttribute('aria-haspopup');
    if (hp !== 'menu') throw new Error(`aria-haspopup="${hp}", expected "menu"`);
  });

  await test('Submenu trigger aria-expanded toggles correctly on open', async () => {
    const sub = page.locator('[data-submenu-trigger="favorites"]').first();
    const before = await sub.getAttribute('aria-expanded');
    if (before !== 'false') throw new Error(`expected "false" before open, got "${before}"`);
    await sub.click();
    await page.waitForTimeout(200);
    const after = await sub.getAttribute('aria-expanded');
    if (after !== 'true') throw new Error(`expected "true" after open, got "${after}"`);
  });

  await ctx.close();
}

// ── + New List event ──────────────────────────────────────────────────────────

async function testNewListEvent() {
  console.log('\n[+ New List event]');
  // favorite_item cards don't show "+ New List" (only product cards do).
  // The FAV_LIST_PATH page type is favorite_item, so we use /recipes... no,
  // recipes don't have favorites submenus either. We need a product card.
  // Products require a search — inject one via the URL search param and wait.
  const ctx = await makeCtx(1440, 900);
  const page = await openPage(ctx, '/products');

  // Trigger a search so product cards render.
  await page.evaluate(() => {
    const alpineRoot = document.querySelector('[x-data*="productBrowser"]')
      || document.querySelector('[x-data*="product"]');
    if (alpineRoot && alpineRoot._x_dataStack) {
      const comp = alpineRoot._x_dataStack[0];
      if (comp && comp.search) comp.search('chicken');
    }
  });

  // Try the search box as a fallback.
  const searchInput = page.locator('input[type="search"], input[placeholder*="earch"]').first();
  const hasSearch = await searchInput.isVisible().catch(() => false);
  if (hasSearch) {
    await searchInput.fill('chicken');
    await searchInput.press('Enter');
    await page.waitForTimeout(3000);
  }

  const triggerVisible = await page.locator('button.action-menu-trigger').first()
    .isVisible().catch(() => false);

  if (!triggerVisible) {
    // No product cards — skip (requires Kroger auth). Document why.
    console.log('  ⚠ SKIP: no product cards loaded (requires Kroger auth for search results)');
    await ctx.close();
    return;
  }

  await test('"+ New List" appears in product favorites submenu', async () => {
    const btn = page.locator('button.action-menu-trigger').first();
    await btn.click();
    await page.waitForTimeout(150);
    const sub = page.locator('[data-submenu-trigger="favorites"]').first();
    await sub.click();
    await page.waitForTimeout(200);
    await page.locator('.action-menu-submenu [role="menuitem"]')
      .filter({ hasText: '+ New List' }).first()
      .waitFor({ state: 'visible', timeout: 3000 });
  });

  await test('Clicking "+ New List" dispatches action-menu:favorites-new-list', async () => {
    await page.evaluate(() => { window.__newListFired = false; });
    await page.evaluate(() => {
      document.addEventListener('action-menu:favorites-new-list',
        () => { window.__newListFired = true; }, { once: true });
    });
    await page.locator('.action-menu-submenu [role="menuitem"]')
      .filter({ hasText: '+ New List' }).first().click();
    await page.waitForTimeout(200);
    const fired = await page.evaluate(() => window.__newListFired);
    if (!fired) throw new Error('action-menu:favorites-new-list event not dispatched');
  });

  await ctx.close();
}

// ── Meal Plan cascade ─────────────────────────────────────────────────────────

async function testMealPlanCascade() {
  console.log('\n[meal plan cascade]');
  const ctx = await makeCtx(1440, 900);
  const page = await openPage(ctx, '/recipes');

  await test('Recipe card Actions menu contains "Add to Meal Plan" leaf', async () => {
    const btn = await firstTrigger(page);
    await btn.click();
    await page.waitForTimeout(200);
    await page.locator('.action-menu-panel [role="menuitem"]')
      .filter({ hasText: 'Add to Meal Plan' }).first()
      .waitFor({ state: 'visible', timeout: 3000 });
  });

  await test('Clicking "Add to Meal Plan" opens the meal plan slide-out panel', async () => {
    const leaf = page.locator('.action-menu-panel [role="menuitem"]')
      .filter({ hasText: 'Add to Meal Plan' }).first();
    await leaf.click();
    await page.waitForTimeout(500);
    // The mealPlanPanel component renders with x-show=open. When open the
    // panel element is visible (offsetParent !== null).
    const panelVisible = await page.locator('[x-data*="mealPlanPanel"]').first()
      .evaluate(el => el.offsetParent !== null).catch(() => false);
    if (!panelVisible) throw new Error('meal plan slide-out panel did not open');
  });

  await ctx.close();
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  browser = await chromium.launch({ headless: true });
  try {
    await testAriaExpandedToggle();
    await testKeyboardTraversal();
    await testFocusReturn();
    await testMobileAccordionBack();
    await testHoverIntentDelay();
    await testSubmenuAriaAttributes();
    await testNewListEvent();
    await testMealPlanCascade();
  } catch (e) {
    console.error('\nFatal:', e.message);
  } finally {
    await browser.close();
  }

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
