// @ts-check
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8000';

/**
 * Playwright E2E tests for ingredient grading system.
 * Tests: recipe list grades, sort pill buttons, recipe detail per-ingredient grades.
 */

let browser, page;
let passed = 0, failed = 0;

function assert(condition, label) {
  if (condition) {
    console.log(`  PASS: ${label}`);
    passed++;
  } else {
    console.log(`  FAIL: ${label}`);
    failed++;
  }
}

async function setup() {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage();
  page.on('console', msg => {
    if (msg.type() === 'error') {
      console.log(`  [console.error] ${msg.text()}`);
    }
  });
}

async function teardown() {
  await browser.close();
  console.log(`\nResults: ${passed} passed, ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
}

// ── TEST 1: Recipe list page shows health grade badges ──
async function testRecipeListGrades() {
  console.log('\nTEST 1: Recipe list health grade badges');
  await page.goto(`${BASE}/recipes`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(600);

  // Health grade badges are spans with x-text="recipe.health_grade"
  const gradeBadges = page.locator('[x-text="recipe.health_grade"]');
  const visibleGrades = await gradeBadges.evaluateAll(els =>
    els.filter(el => el.offsetParent !== null && el.textContent.trim()).map(el => el.textContent.trim())
  );

  assert(visibleGrades.length > 0, `Found ${visibleGrades.length} visible health grade badges on recipe cards`);

  // Grades should be valid letters
  const validGrades = ['A', 'B', 'C', 'D', 'F'];
  const allValid = visibleGrades.every(g => validGrades.includes(g));
  assert(allValid, `All grade values are valid (A-F): ${[...new Set(visibleGrades)].join(', ')}`);

  // Check that grade badges have colored backgrounds (gradeColor applied)
  const withBg = await gradeBadges.evaluateAll(els =>
    els.filter(el => el.offsetParent !== null && el.style.background).length
  );
  assert(withBg > 0, `${withBg} grade badges have colored backgrounds`);
}

// ── TEST 2: Sort pill buttons work (toggle sort by health) ──
async function testSortPills() {
  console.log('\nTEST 2: Sort pill buttons');
  await page.goto(`${BASE}/recipes`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(600);

  // Sort pills are rendered from sortOptions via x-for
  // Find the "Healthiest First" pill button
  const healthPill = page.locator('button:has-text("Healthiest First")');
  const pillVisible = await healthPill.isVisible().catch(() => false);
  assert(pillVisible, 'Healthiest First sort pill is visible');

  if (pillVisible) {
    // Click it — should toggle active state and show Clear button
    await healthPill.click();
    await page.waitForTimeout(500);

    // After click, pill should be in the sortStack → styled with green background
    const pillStyle = await healthPill.getAttribute('style');
    assert(
      pillStyle && pillStyle.includes('var(--green)'),
      'Healthiest pill is active (green) after click'
    );

    // Clear button should appear (exact match to avoid "Clear all" in tag filter)
    const clearBtn = page.getByRole('button', { name: 'Clear', exact: true });
    const clearVisible = await clearBtn.isVisible().catch(() => false);
    assert(clearVisible, 'Clear button appears when sort pill is active');

    // Click Clear to reset
    if (clearVisible) {
      await clearBtn.click();
      await page.waitForTimeout(500);
      const clearGone = !(await clearBtn.isVisible().catch(() => false));
      assert(clearGone, 'Clear button disappears after clearing sort');
    }
  }
}

// ── TEST 3: Recipe detail page shows per-ingredient grades ──
async function testRecipeDetailGrades() {
  console.log('\nTEST 3: Recipe detail per-ingredient grades');
  await page.goto(`${BASE}/recipes`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(600);

  // Click the first recipe card link
  const firstRecipeLink = page.locator('a[href^="/recipes/"]').first();
  const linkVisible = await firstRecipeLink.isVisible().catch(() => false);
  assert(linkVisible, 'Recipe link visible on list page');

  if (linkVisible) {
    await firstRecipeLink.click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(600);

    // Should be on recipe detail page
    const url = page.url();
    assert(url.includes('/recipes/'), `Navigated to recipe detail: ${url}`);

    // Check for overall health badge (e.g. "Health: A (100/100)")
    const healthBadge = page.locator('text=/Health: [A-F]/');
    const healthBadgeVisible = await healthBadge.isVisible().catch(() => false);
    assert(healthBadgeVisible, 'Overall health grade badge is visible');

    // Check for per-ingredient safety grade badges
    const ingGrades = page.locator('[x-text="ing.safety_grade"]');
    const visibleIngGrades = await ingGrades.evaluateAll(els =>
      els.filter(el => el.offsetParent !== null && el.textContent.trim()).map(el => el.textContent.trim())
    );

    assert(visibleIngGrades.length > 0, `Found ${visibleIngGrades.length} per-ingredient grade badges`);

    // Check that grades are varied — not all C (the original bug)
    const uniqueIngGrades = [...new Set(visibleIngGrades)];
    const allC = uniqueIngGrades.length === 1 && uniqueIngGrades[0] === 'C';
    assert(!allC, `Per-ingredient grades are not all C: ${uniqueIngGrades.join(', ')}`);

    // Should have some A or B grades for clean ingredients
    const hasGoodGrades = visibleIngGrades.some(g => g === 'A' || g === 'B');
    assert(hasGoodGrades, 'Some ingredients have A or B grades (healthy whole foods)');

    // Verify grade colors are applied
    const gradeWithBg = await ingGrades.evaluateAll(els =>
      els.filter(el => el.offsetParent !== null && el.style.background).length
    );
    assert(gradeWithBg > 0, `${gradeWithBg} grade badges have colored backgrounds`);
  }
}

// ── TEST 4: Ingredient table structure in recipe detail ──
async function testIngredientTableStructure() {
  console.log('\nTEST 4: Ingredient table structure');
  // Navigate to a recipe detail page
  await page.goto(`${BASE}/recipes`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(600);

  const firstRecipeLink = page.locator('a[href^="/recipes/"]').first();
  if (await firstRecipeLink.isVisible().catch(() => false)) {
    await firstRecipeLink.click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(600);

    // Check column headers
    const headers = ['Qty', 'Ingredient', 'Source', 'Health'];
    for (const h of headers) {
      const el = page.locator(`text="${h}"`).first();
      const vis = await el.isVisible().catch(() => false);
      assert(vis, `"${h}" column header visible`);
    }

    // Check that ingredient rows exist
    const ingRows = page.locator('[x-text="ing.name"]');
    const count = await ingRows.evaluateAll(els =>
      els.filter(el => el.offsetParent !== null).length
    );
    assert(count > 0, `${count} ingredient rows rendered`);
  }
}

// ── TEST 5: No JavaScript errors on recipe pages ──
async function testNoJSErrors() {
  console.log('\nTEST 5: No JavaScript errors');
  const errors = [];
  const errorPage = await browser.newPage();
  errorPage.on('console', msg => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  errorPage.on('pageerror', err => errors.push(err.message));

  await errorPage.goto(`${BASE}/recipes`);
  await errorPage.waitForLoadState('networkidle');
  await errorPage.waitForTimeout(1000);

  // Filter out known non-critical errors
  const criticalErrors = errors.filter(e =>
    !e.includes('favicon') && !e.includes('404')
  );

  assert(criticalErrors.length === 0,
    criticalErrors.length === 0
      ? 'No JavaScript errors on recipes list page'
      : `JS errors found: ${criticalErrors.join('; ')}`
  );

  // Also test recipe detail page
  const errors2 = [];
  const detailPage = await browser.newPage();
  detailPage.on('console', msg => {
    if (msg.type() === 'error') errors2.push(msg.text());
  });
  detailPage.on('pageerror', err => errors2.push(err.message));

  // Navigate to first recipe detail
  await detailPage.goto(`${BASE}/recipes`);
  await detailPage.waitForLoadState('networkidle');
  await detailPage.waitForTimeout(500);
  const link = detailPage.locator('a[href^="/recipes/"]').first();
  if (await link.isVisible().catch(() => false)) {
    await link.click();
    await detailPage.waitForLoadState('networkidle');
    await detailPage.waitForTimeout(1000);
  }

  const criticalErrors2 = errors2.filter(e =>
    !e.includes('favicon') && !e.includes('404')
  );
  assert(criticalErrors2.length === 0,
    criticalErrors2.length === 0
      ? 'No JavaScript errors on recipe detail page'
      : `JS errors on detail: ${criticalErrors2.join('; ')}`
  );

  await errorPage.close();
  await detailPage.close();
}

(async () => {
  try {
    await setup();
    await testRecipeListGrades();
    await testSortPills();
    await testRecipeDetailGrades();
    await testIngredientTableStructure();
    await testNoJSErrors();
  } catch (err) {
    console.error(`\nTest runner error: ${err.message}`);
    failed++;
  } finally {
    await teardown();
  }
})();
