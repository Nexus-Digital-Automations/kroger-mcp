// @ts-check
/**
 * Playwright E2E: recipe detail ingredient quantities + servings scaling.
 * Tests multiple recipes to catch both numeric and string quantity formats.
 */

const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8000';

let browser, page;
let passed = 0, failed = 0;
const consoleErrors = [];

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
      const text = msg.text();
      consoleErrors.push(text);
      console.log(`  [console.error] ${text}`);
    }
  });
  page.on('pageerror', err => {
    consoleErrors.push(err.message);
    console.log(`  [pageerror] ${err.message}`);
  });
}

async function getIngredientRows() {
  // Rows carry the data-ingredient-row hook; cells: span[0]=qty, span[1]=unit,
  // span[2]=name (later spans belong to source/health/popovers).
  return page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    if (!card) return [];
    const rows = Array.from(card.querySelectorAll('[data-ingredient-row]'));
    return rows.map(row => {
      const spans = row.querySelectorAll('span');
      return {
        qty: spans[0] ? spans[0].textContent.trim() : '',
        unit: spans[1] ? spans[1].textContent.trim() : '',
        name: spans[2] ? spans[2].textContent.trim() : '',
      };
    });
  });
}

async function runTests() {
  await setup();
  console.log('\n=== Recipe Ingredients E2E Test ===\n');

  // Get all recipe links
  await page.goto(`${BASE}/recipes`, { waitUntil: 'networkidle' });
  const recipeHrefs = await page.evaluate(() => {
    return Array.from(document.querySelectorAll('a[href^="/recipes/"]'))
      .map(l => l.getAttribute('href')).filter(Boolean);
  });
  assert(recipeHrefs.length > 0, `Found ${recipeHrefs.length} recipe links`);

  // Test up to 5 recipes — look for one with many ingredients that have quantities
  const sampled = recipeHrefs.slice(0, Math.min(5, recipeHrefs.length));
  let bestRecipe = null;
  let bestQtyCount = 0;

  for (const href of sampled) {
    await page.goto(`${BASE}${href}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1200);
    const rows = await getIngredientRows();
    const withQty = rows.filter(r => r.qty.length > 0 && r.name.length > 0);
    console.log(`  Recipe ${href}: ${rows.length} rows, ${withQty.length} with qty`);
    if (withQty.length > bestQtyCount) {
      bestQtyCount = withQty.length;
      bestRecipe = { href, rows, withQty };
    }
  }

  assert(bestRecipe !== null, 'Found at least one recipe with ingredients');
  if (!bestRecipe) { await browser.close(); return; }

  console.log(`\n  Best recipe: ${bestRecipe.href} (${bestQtyCount} rows with quantities)`);
  assert(bestQtyCount > 0, `At least 1 ingredient has a visible quantity`);

  // Check the best recipe in detail
  await page.goto(`${BASE}${bestRecipe.href}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);

  const rows = await getIngredientRows();
  const withQty = rows.filter(r => r.qty.length > 0);
  const withoutQty = rows.filter(r => r.qty.length === 0 && r.name.length > 0);

  console.log(`\n  First 5 ingredient rows:`);
  rows.slice(0, 5).forEach((r, i) => console.log(`    [${i}] qty="${r.qty}" name="${r.name}"`));

  // The ratio of rows WITH a quantity should be reasonable (not 0)
  assert(withQty.length > 0, `Quantities visible: ${withQty.length}/${rows.length} rows have qty`);
  if (withoutQty.length > 0) {
    console.log(`  INFO: ${withoutQty.length} rows have no qty (likely null in data — expected)`);
    withoutQty.slice(0, 3).forEach(r => console.log(`    no-qty: "${r.name}"`));
  }

  // Footer button must be gone
  const noFooterBtn = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    if (!card) return true;
    return !Array.from(card.querySelectorAll('button'))
      .some(b => b.textContent.includes('Add to Shopping List'));
  });
  assert(noFooterBtn, '"Add to Shopping List" footer button absent from ingredients card');

  // Stepper: inc() via Alpine, verify quantities update
  const qtyBefore = rows[0] ? rows[0].qty : '';
  const servingsBefore = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    return card && card._x_dataStack ? card._x_dataStack[0].servings : null;
  });

  if (servingsBefore && withQty.length > 0) {
    // Double the servings
    for (let i = 0; i < servingsBefore; i++) {
      await page.evaluate(() => {
        const card = document.getElementById('ingredients-card');
        if (card && card._x_dataStack) card._x_dataStack[0].inc();
      });
      await page.waitForTimeout(30);
    }
    await page.waitForTimeout(300);

    const rowsAfter = await getIngredientRows();
    const qtyAfter = rowsAfter[0] ? rowsAfter[0].qty : '';
    console.log(`\n  Stepper: servings ${servingsBefore} → ${servingsBefore * 2}, first qty "${qtyBefore}" → "${qtyAfter}"`);

    // For a numeric quantity, it should change. For a string like "4 strips" it stays the same.
    // We just verify no JS errors occurred during scaling.
    const firstRowHasQty = withQty.length > 0;
    if (firstRowHasQty && !isNaN(parseFloat(qtyBefore))) {
      assert(qtyAfter !== qtyBefore, `Numeric qty scaled: "${qtyBefore}" → "${qtyAfter}"`);
    } else {
      console.log('  INFO: First qty is non-numeric or empty, skipping scale assertion');
    }
  }

  // No console errors throughout
  assert(consoleErrors.length === 0, `No JavaScript errors (${consoleErrors.length} found)`);

  await browser.close();
  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
  process.exit(failed > 0 ? 1 : 0);
}

runTests().catch(err => {
  console.error('Test runner error:', err);
  process.exit(1);
});
