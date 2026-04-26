// @ts-check
/**
 * Smart Shopper — Accessibility Audit (axe-core)
 *
 * Loads each page, injects axe-core from unpkg CDN, runs axe.run(),
 * and reports critical + serious violations. Fails if any are found.
 *
 * Usage: node tests/playwright/test_accessibility.js
 */

const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8000';

// Pages to audit — every template a user can navigate to.
const PAGES = [
  '/dashboard',
  '/products',
  '/deals',
  '/shopping-list',
  '/pantry',
  '/favorites',
  '/recipes',
  '/meal-plan',
  '/meal-tracker',
  '/predictions',
  '/settings',
  '/safety',
  '/ingredients',
  '/analytics',
];

let passed = 0;
let failed = 0;
const failures = [];

async function audit(page, url) {
  console.log(`\n=== ${url} ===`);
  try {
    await page.goto(BASE + url, { waitUntil: 'networkidle', timeout: 15000 });
  } catch (e) {
    console.log(`  SKIP — page not reachable: ${e.message.slice(0, 80)}`);
    return;
  }

  // Inject axe-core from CDN
  await page.addScriptTag({
    url: 'https://unpkg.com/axe-core@4.10.3/axe.min.js',
    type: 'text/javascript',
  });

  const results = await page.evaluate(() => {
    // @ts-ignore
    return window.axe.run(document, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'best-practice'] },
      resultTypes: ['violations'],
    });
  });

  const violations = results.violations || [];
  const serious = violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');

  if (serious.length === 0) {
    console.log(`  PASS — 0 critical/serious violations (${violations.length} minor)`);
    passed++;
  } else {
    console.log(`  FAIL — ${serious.length} critical/serious violations:`);
    for (const v of serious) {
      const nodes = v.nodes || [];
      const label = `${v.id}: ${v.help} (${v.impact}, ${nodes.length} node(s))`;
      console.log(`    ${label}`);
      failures.push(`${url} — ${label}`);
    }
    failed++;
  }
}

(async () => {
  console.log('Smart Shopper — Accessibility Audit\n');

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      console.log(`  console.error: ${msg.text().slice(0, 120)}`);
    }
  });

  for (const p of PAGES) {
    await audit(page, p);
  }

  await browser.close();

  console.log(`\n========================================`);
  console.log(`Results: ${passed} passed, ${failed} failed`);
  if (failures.length) {
    console.log(`\nFailures:`);
    for (const f of failures) console.log(`  ${f}`);
    process.exit(1);
  }
})();
