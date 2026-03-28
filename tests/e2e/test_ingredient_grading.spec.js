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

// ── TEST 2: Two-zone ranked sort UI ──
async function testSortPills() {
  console.log('\nTEST 2: Ranked sort UI');
  await page.goto(`${BASE}/recipes`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(600);

  // Zone A: "Sort" label and text-style toggle buttons
  const sortLabel = page.locator('text="Sort"').first();
  assert(await sortLabel.isVisible().catch(() => false), 'Sort label visible in Zone A');

  const healthBtn = page.locator('button:has-text("Healthiest First")');
  assert(await healthBtn.isVisible().catch(() => false), 'Healthiest First option visible in Zone A');

  // Click to activate — Zone B should appear with rank 1 chip
  await healthBtn.click();
  await page.waitForTimeout(500);

  const sortingByLabel = page.locator('text="Sorting by"').first();
  assert(await sortingByLabel.isVisible().catch(() => false), 'Zone B appears with "Sorting by" label');

  const firstBadge = page.locator('text="1st"').first();
  assert(await firstBadge.isVisible().catch(() => false), '1st rank ordinal badge visible');

  // Click a second sort — should appear as rank 2
  const costBtn = page.locator('button:has-text("Cost")').first();
  await costBtn.click();
  await page.waitForTimeout(500);

  const secondBadge = page.locator('text="2nd"').first();
  assert(await secondBadge.isVisible().catch(() => false), '2nd rank ordinal badge visible for sub-sort');

  // Promote: click visible up arrow (rank 2 chip has it)
  const upArrows = page.locator('button[title="Move up in priority"]');
  const visibleUpArrow = await upArrows.evaluateAll(els =>
    els.findIndex(el => el.offsetParent !== null)
  );
  if (visibleUpArrow >= 0) {
    await upArrows.nth(visibleUpArrow).click({ force: true });
    await page.waitForTimeout(500);
    // After promote, the chip order should have swapped
    const chips = await page.locator('[x-text="sortKeyLabel(key)"]').evaluateAll(els =>
      els.filter(el => el.offsetParent !== null).map(el => el.textContent.trim())
    );
    assert(chips.length === 2, `Two ranked chips after promote: ${chips.join(', ')}`);
  } else {
    assert(false, 'Up arrow visible for rank 2 chip');
  }

  // Remove: click first remove button
  const removeBtn = page.locator('button[title="Remove sort"]');
  const visibleRemove = await removeBtn.evaluateAll(els =>
    els.findIndex(el => el.offsetParent !== null)
  );
  if (visibleRemove >= 0) {
    await removeBtn.nth(visibleRemove).click({ force: true });
    await page.waitForTimeout(500);
    const remainingChips = await page.locator('[x-text="sortKeyLabel(key)"]').evaluateAll(els =>
      els.filter(el => el.offsetParent !== null).length
    );
    assert(remainingChips === 1, `One chip remaining after remove`);
  }

  // Clear all
  const clearAll = page.getByRole('button', { name: 'Clear all' });
  const clearVisible = await clearAll.isVisible().catch(() => false);
  assert(clearVisible, 'Clear all button visible');
  if (clearVisible) {
    await clearAll.click();
    await page.waitForTimeout(500);
    const zoneBHidden = !(await sortingByLabel.isVisible().catch(() => false));
    assert(zoneBHidden, 'Zone B hidden after Clear all');
  }
}

// ── TEST 3: Recipe detail page shows per-ingredient grades ──
async function testRecipeDetailGrades() {
  console.log('\nTEST 3: Recipe detail per-ingredient grades');

  // Get a recipe ID from the list page, then navigate directly
  await page.goto(`${BASE}/recipes`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(600);

  const recipeHref = await page.locator('.grid a[href^="/recipes/"]').first().getAttribute('href');
  assert(!!recipeHref, `Found recipe link: ${recipeHref}`);

  await page.goto(`${BASE}${recipeHref}`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(800);

  const url = page.url();
  assert(url.includes('/recipes/'), `On recipe detail page: ${url}`);

  // Check for overall health badge (Alpine-rendered via x-text)
  const healthText = await page.evaluate(() => {
    const els = document.querySelectorAll('[x-text]');
    for (const el of els) {
      if (el.textContent && el.textContent.match(/Health:\s*[A-F]/)) return el.textContent.trim();
    }
    return null;
  });
  assert(!!healthText, `Overall health badge rendered: ${healthText || 'not found'}`);

  // Check for per-ingredient safety grade badges
  const ingGrades = await page.evaluate(() => {
    const els = document.querySelectorAll('[x-text="ing.safety_grade"]');
    return [...els].filter(el => el.offsetParent !== null && el.textContent.trim())
      .map(el => el.textContent.trim());
  });

  assert(ingGrades.length > 0, `Found ${ingGrades.length} per-ingredient grade badges`);

  // Check that grades are varied — not all C (the original bug)
  const uniqueIngGrades = [...new Set(ingGrades)];
  const allC = uniqueIngGrades.length === 1 && uniqueIngGrades[0] === 'C';
  assert(!allC, `Per-ingredient grades are not all C: ${uniqueIngGrades.join(', ')}`);

  // Should have some A or B grades for clean ingredients
  const hasGoodGrades = ingGrades.some(g => g === 'A' || g === 'B');
  assert(hasGoodGrades, 'Some ingredients have A or B grades (healthy whole foods)');
}

// ── TEST 4: Ingredient table structure in recipe detail ──
async function testIngredientTableStructure() {
  console.log('\nTEST 4: Ingredient table structure');
  // Should still be on recipe detail from test 3 — verify or navigate
  if (!page.url().match(/\/recipes\/\w/)) {
    await page.goto(`${BASE}/recipes`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(600);
    const href = await page.locator('.grid a[href^="/recipes/"]').first().getAttribute('href');
    await page.goto(`${BASE}${href}`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(800);
  }

  // Check column headers
  const headers = ['Qty', 'Ingredient', 'Source', 'Health'];
  for (const h of headers) {
    const vis = await page.evaluate((text) => {
      const els = document.querySelectorAll('span');
      return [...els].some(el => el.textContent.trim() === text && el.offsetParent !== null);
    }, h);
    assert(vis, `"${h}" column header visible`);
  }

  // Check that ingredient rows exist
  const count = await page.evaluate(() => {
    const els = document.querySelectorAll('[x-text="ing.name"]');
    return [...els].filter(el => el.offsetParent !== null).length;
  });
  assert(count > 0, `${count} ingredient rows rendered`);
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

  // Navigate to first recipe detail via direct URL
  await detailPage.goto(`${BASE}/recipes`);
  await detailPage.waitForLoadState('networkidle');
  await detailPage.waitForTimeout(600);
  const dHref = await detailPage.locator('.grid a[href^="/recipes/"]').first().getAttribute('href').catch(() => null);
  if (dHref) {
    await detailPage.goto(`${BASE}${dHref}`);
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
