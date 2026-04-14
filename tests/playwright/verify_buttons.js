// Quick verification: check what buttons are visible on the recipes page
const { chromium } = require('playwright');

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errs = [];
  page.on('console', msg => { if (msg.type() === 'error') errs.push(msg.text()); });

  await page.goto('http://127.0.0.1:8000/recipes', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500); // wait for Alpine to render x-for

  // Count all buttons
  const allBtns = await page.locator('button').all();
  const btnTexts = [];
  for (const btn of allBtns.slice(0, 20)) {
    const text = (await btn.textContent()).trim().replace(/\s+/g, ' ').slice(0, 40);
    if (text) btnTexts.push(text);
  }
  console.log('First 20 button texts:', btnTexts);

  // Check specifically for our new buttons
  const listBtns = await page.locator('button:has-text("List")').count();
  const mealBtns = await page.locator('button:has-text("Meal Plan")').count();
  const addMealBtns = await page.locator('button:has-text("Add to Meal Plan")').count();
  console.log(`\nButton counts:`);
  console.log(`  "List" buttons: ${listBtns}`);
  console.log(`  "Meal Plan" buttons: ${mealBtns}`);
  console.log(`  "Add to Meal Plan" buttons: ${addMealBtns}`);

  // Check for JS errors
  if (errs.length > 0) {
    console.log('\nJS Errors:');
    errs.slice(0, 5).forEach(e => console.log(' -', e.slice(0, 150)));
  } else {
    console.log('\nNo JS errors');
  }

  // Screenshot
  await page.screenshot({ path: 'tests/playwright/screenshots/verify_buttons.png' });
  await browser.close();
}

main().catch(console.error);
