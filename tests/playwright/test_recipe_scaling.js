// @ts-check
/**
 * Playwright E2E: ingredient scaling correctness on the recipe detail page.
 *
 * Verifies the two bugs we set out to fix:
 *  1. The scaled quantity must never silently fall back to the unscaled
 *     original (old line 322 had `|| String(ing.quantity)`).
 *  2. fmtQty must never emit '' / '0' from a positive scaled value.
 *
 * Also exercises the new "Reset to base" button.
 */

const { chromium } = require('playwright');

const BASE = process.env.SS_TEST_BASE || 'http://127.0.0.1:8000';

let browser, page;
let passed = 0, failed = 0;
const consoleErrors = [];

function assert(cond, label) {
  if (cond) { console.log(`  PASS: ${label}`); passed++; }
  else      { console.log(`  FAIL: ${label}`); failed++; }
}

async function setup() {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage();
  page.on('console', msg => {
    if (msg.type() === 'error') {
      consoleErrors.push(msg.text());
      console.log(`  [console.error] ${msg.text()}`);
    }
  });
  page.on('pageerror', err => {
    consoleErrors.push(err.message);
    console.log(`  [pageerror] ${err.message}`);
  });
}

async function readRows() {
  return page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    if (!card) return null;
    const stack = card._x_dataStack;
    if (!stack || !stack[0]) return null;
    const panel = stack[0];
    const rows = Array.from(card.querySelectorAll('[data-ingredient-row]'));
    return {
      servings: panel.servings,
      baseServings: panel.baseServings,
      ings: panel.ings.map(i => ({
        name: i.name, quantity: i.quantity, unit: i.unit,
      })),
      display: rows.map(row => {
        const spans = row.querySelectorAll('span');
        return {
          qty: spans[0] ? spans[0].textContent.trim() : '',
          unit: spans[1] ? spans[1].textContent.trim() : '',
          name: spans[2] ? spans[2].textContent.trim() : '',
        };
      }),
    };
  });
}

async function pickRecipeWithNumericQty() {
  await page.goto(`${BASE}/recipes`, { waitUntil: 'networkidle' });
  const hrefs = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a[href^="/recipes/"]'))
      .map(l => l.getAttribute('href')).filter(Boolean)
  );
  for (const href of hrefs.slice(0, 10)) {
    await page.goto(`${BASE}${href}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    const state = await readRows();
    if (!state) continue;
    const numericCount = state.ings.filter(i =>
      i.quantity !== null && i.quantity !== undefined && Number.isFinite(+i.quantity) && +i.quantity > 0
    ).length;
    if (numericCount >= 1) return { href, state };
  }
  return null;
}

async function setServings(target) {
  await page.evaluate((t) => {
    const card = document.getElementById('ingredients-card');
    if (card && card._x_dataStack) card._x_dataStack[0].servings = t;
  }, target);
  await page.waitForTimeout(120);
}

async function runTests() {
  await setup();
  console.log('\n=== Recipe Ingredient Scaling E2E ===\n');

  const picked = await pickRecipeWithNumericQty();
  assert(picked !== null, 'Located a recipe with at least one numeric ingredient quantity');
  if (!picked) { await browser.close(); process.exit(failed > 0 ? 1 : 0); return; }

  const { href, state: base } = picked;
  console.log(`  Using ${href} (base servings = ${base.baseServings})\n`);
  console.log(`  Base ingredients (first 5):`);
  base.display.slice(0, 5).forEach((r, i) => console.log(`    [${i}] qty="${r.qty}" unit="${r.unit}" name="${r.name}"`));

  // Capture base display for comparison.
  const baseQtyDisplay = base.display.map(r => r.qty);

  // ── Double ── verify numeric quantities roughly doubled, none unchanged.
  await setServings(base.baseServings * 2);
  const doubled = await readRows();
  assert(doubled.servings === base.baseServings * 2, `Servings stepped to ${doubled.servings}`);

  const numericIdxs = base.ings
    .map((i, n) => ({ q: +i.quantity, n }))
    .filter(x => Number.isFinite(x.q) && x.q > 0)
    .map(x => x.n);

  let scaledChanges = 0;
  for (const n of numericIdxs) {
    const before = baseQtyDisplay[n];
    const after  = doubled.display[n] ? doubled.display[n].qty : '';
    if (after && after !== before) scaledChanges++;
  }
  assert(scaledChanges >= 1, `At least one numeric qty changed after doubling (${scaledChanges}/${numericIdxs.length})`);

  // Regression guard for the fallback bug: no doubled-row display may equal
  // the *exact* base display text for a numeric row (it must have scaled).
  let fallbacks = 0;
  for (const n of numericIdxs) {
    const before = baseQtyDisplay[n];
    const after  = doubled.display[n] ? doubled.display[n].qty : '';
    if (after && before && after === before) fallbacks++;
  }
  assert(fallbacks === 0, `No numeric row falls back to unscaled value (${fallbacks} regressions)`);

  // ── Reset button ── put servings back to base.
  const resetClicked = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('button'));
    const btn = btns.find(b => b.textContent.trim() === 'Reset');
    if (btn) { btn.click(); return true; }
    return false;
  });
  assert(resetClicked, '"Reset" button is present and clickable');
  await page.waitForTimeout(150);

  const afterReset = await readRows();
  assert(afterReset.servings === base.baseServings, `Reset restored servings to ${base.baseServings}`);
  // Display should match base for numeric rows.
  let resetMatches = 0;
  for (const n of numericIdxs) {
    if (afterReset.display[n] && afterReset.display[n].qty === baseQtyDisplay[n]) resetMatches++;
  }
  assert(resetMatches === numericIdxs.length, `Reset restored all numeric qty displays (${resetMatches}/${numericIdxs.length})`);

  // ── fmtQty unit checks via the live Alpine component.
  const fmt = await page.evaluate(() => {
    const card = document.getElementById('ingredients-card');
    const panel = card && card._x_dataStack && card._x_dataStack[0];
    if (!panel) return null;
    const f = panel.fmtQty.bind(panel);
    return {
      null:    f(null),
      undef:   f(undefined),
      zero:    f(0),
      one:     f(1),
      half:    f(0.5),
      quarter: f(0.25),
      twoThirds: f(0.6667),
      oneSixth: f(0.1667),
      whole2:  f(2),
      mixed:   f(1.5),
      tiny:    f(0.03),
      veryTiny: f(0.001),
    };
  });
  assert(fmt !== null, 'fmtQty reachable on the live Alpine component');
  if (fmt) {
    assert(fmt.null === '', 'fmtQty(null) -> ""');
    assert(fmt.zero === '0', 'fmtQty(0) -> "0" (explicit, not silently dropped)');
    assert(fmt.one === '1', 'fmtQty(1) -> "1"');
    assert(fmt.half === '½', `fmtQty(0.5) -> "½" (got "${fmt.half}")`);
    assert(fmt.quarter === '¼', `fmtQty(0.25) -> "¼" (got "${fmt.quarter}")`);
    assert(fmt.twoThirds === '⅔', `fmtQty(0.6667) -> "⅔" (got "${fmt.twoThirds}")`);
    assert(fmt.whole2 === '2', `fmtQty(2) -> "2" (got "${fmt.whole2}")`);
    // fmtQty separates the whole and fraction with a thin space (U+2009).
    assert(fmt.mixed === '1\u2009\u00bd', `fmtQty(1.5) -> 1 U+2009 ½ (got "${fmt.mixed}")`);
    // 0.03 must not collapse to '' or '0' — that was the silent-zero bug.
    assert(fmt.tiny !== '' && fmt.tiny !== '0', `fmtQty(0.03) does not collapse (got "${fmt.tiny}")`);
    assert(fmt.veryTiny !== '' && fmt.veryTiny !== '0', `fmtQty(0.001) does not collapse (got "${fmt.veryTiny}")`);
  }

  assert(consoleErrors.length === 0, `No JavaScript errors (${consoleErrors.length} found)`);

  await browser.close();
  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
  process.exit(failed > 0 ? 1 : 0);
}

runTests().catch(err => {
  console.error('Test runner error:', err);
  process.exit(1);
});
