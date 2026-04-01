// @ts-check
/**
 * Playwright diagnostic: recipe detail ingredient quantities + servings scaling.
 */

const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8080';

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

async function getFirstRecipeId() {
  await page.goto(`${BASE}/recipes`, { waitUntil: 'networkidle' });
  // Find first recipe link
  const href = await page.evaluate(() => {
    const links = Array.from(document.querySelectorAll('a[href^="/recipes/"]'));
    return links.length > 0 ? links[0].getAttribute('href') : null;
  });
  return href;
}

async function runTests() {
  await setup();

  console.log('\n=== Recipe Ingredients E2E Test ===\n');

  // 1. Find a recipe to test with
  const recipeHref = await getFirstRecipeId();
  assert(recipeHref !== null, `Found a recipe link: ${recipeHref}`);
  if (!recipeHref) {
    console.log('No recipes found — cannot continue');
    await browser.close();
    return;
  }

  // 2. Navigate to recipe detail
  console.log(`\n  → Navigating to ${BASE}${recipeHref}`);
  await page.goto(`${BASE}${recipeHref}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500); // let Alpine fully initialize

  // 3. Check ingredients card exists
  const card = await page.$('#ingredients-card');
  assert(card !== null, 'Ingredients card (#ingredients-card) is present');

  // 4. Check ingredient rows are rendered
  const rowCount = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    if (!card) return 0;
    // Count visible divs that look like ingredient rows
    const rows = card.querySelectorAll('[style*="grid-template-columns: 7rem"]');
    return rows.length;
  });
  console.log(`  INFO: Found ${rowCount} grid rows (including column header)`);
  assert(rowCount > 1, 'At least 1 ingredient row rendered');

  // 5. Check quantity text content
  const qtyTexts = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    if (!card) return [];
    // Get all spans that are in the quantity column position
    const rows = Array.from(card.querySelectorAll('[style*="grid-template-columns: 7rem"]'));
    return rows.slice(1, 6).map(row => { // skip column header row
      const spans = row.querySelectorAll('span');
      return {
        qty: spans[0] ? spans[0].textContent.trim() : '(no span)',
        name: spans[1] ? spans[1].textContent.trim() : '(no span)',
      };
    });
  });

  console.log('\n  Ingredient rows (first 5):');
  qtyTexts.forEach((r, i) => console.log(`    [${i}] qty="${r.qty}" name="${r.name}"`));

  const nonEmptyQtys = qtyTexts.filter(r => r.qty.length > 0 && r.name.length > 0);
  assert(nonEmptyQtys.length > 0, `At least 1 ingredient has a non-empty quantity`);

  // 6. Log Alpine component state for diagnosis
  const alpineState = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    if (!card || !card._x_dataStack) return 'no Alpine data stack';
    const data = card._x_dataStack[0];
    return {
      servings: data.servings,
      baseServings: data.baseServings,
      ingsLength: data.ings ? data.ings.length : 0,
      firstIngQty: data.ings && data.ings[0] ? data.ings[0].quantity : null,
      fmtQtyExists: typeof data.fmtQty === 'function',
      fmtQtySample: typeof data.fmtQty === 'function' ? data.fmtQty(4.5) : '(not a function)',
    };
  });
  console.log('\n  Alpine component state:');
  console.log('   ', JSON.stringify(alpineState, null, 2).replace(/\n/g, '\n    '));

  // 7. Stepper: check servings stepper buttons exist
  const stepperExists = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    if (!card) return false;
    const btns = card.querySelectorAll('button');
    return btns.length >= 2;
  });
  assert(stepperExists, 'Servings stepper buttons present');

  // 8. Test no "Add to Shopping List" footer button in the card
  const noFooterBtn = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    if (!card) return true;
    const btns = Array.from(card.querySelectorAll('button'));
    // Stepper buttons contain "−" and "+"
    const addBtn = btns.find(b => b.textContent.includes('Add to Shopping List'));
    return addBtn === undefined;
  });
  assert(noFooterBtn, '"Add to Shopping List" footer button is removed from ingredients card');

  // 9. Stepper interaction: click + and verify quantities double (at 2x)
  const qtyBefore = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    const rows = Array.from(card.querySelectorAll('[style*="grid-template-columns: 7rem"]'));
    return rows[1] ? rows[1].querySelectorAll('span')[0].textContent.trim() : '';
  });

  const servingsBefore = alpineState.servings;

  // Click + servingsBefore times via Alpine's inc() to double servings
  for (let i = 0; i < servingsBefore; i++) {
    await page.evaluate(() => {
      const card = document.getElementById('ingredients-card');
      if (card && card._x_dataStack) card._x_dataStack[0].inc();
    });
    await page.waitForTimeout(50);
  }
  await page.waitForTimeout(300);

  const qtyAfter = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    const rows = Array.from(card.querySelectorAll('[style*="grid-template-columns: 7rem"]'));
    return rows[1] ? rows[1].querySelectorAll('span')[0].textContent.trim() : '';
  });

  console.log(`\n  Stepper scaling: before="${qtyBefore}" after="${qtyAfter}" (doubled servings from ${servingsBefore} to ${servingsBefore * 2})`);
  assert(qtyBefore !== qtyAfter && qtyAfter.length > 0, `Quantities update when servings change (was "${qtyBefore}", now "${qtyAfter}")`);

  // 10. Console errors check
  assert(consoleErrors.length === 0, `No JavaScript console errors (found: ${consoleErrors.length})`);
  if (consoleErrors.length > 0) {
    console.log('  Errors:');
    consoleErrors.forEach(e => console.log(`    - ${e}`));
  }

  await browser.close();

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
  process.exit(failed > 0 ? 1 : 0);
}

runTests().catch(err => {
  console.error('Test runner error:', err);
  process.exit(1);
});
