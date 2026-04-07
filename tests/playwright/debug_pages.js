// Quick check of failing page content
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8080';

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  for (const p of ['/meal-tracker', '/favorites', '/products', '/safety']) {
    await page.goto(`${BASE}${p}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600);
    const title = await page.title();
    const h1 = await page.locator('h1, h2, [class*="heading"]').first().textContent().catch(() => 'NONE');
    const heading = await page.evaluate(() => {
      const el = document.querySelector('main h1, main h2, h1, h2');
      return el ? el.textContent.trim() : 'NOT FOUND';
    });
    console.log(`${p}:`);
    console.log(`  title: ${title}`);
    console.log(`  heading: ${heading}`);
    console.log(`  h1/h2: ${h1.trim()}`);
  }

  // Check favorites detail page
  await page.goto(`${BASE}/favorites`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(500);
  const links = await page.locator('a[href^="/favorites/"]').all();
  if (links.length > 0) {
    const href = await links[0].getAttribute('href');
    await page.goto(`${BASE}${href}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(500);
    const title = await page.title();
    const heading = await page.evaluate(() => {
      const el = document.querySelector('main h1, main h2, h1, h2');
      return el ? el.textContent.trim() : 'NOT FOUND';
    });
    console.log(`\nFavorites Detail (${href}):`);
    console.log(`  title: ${title}`);
    console.log(`  heading: ${heading}`);
  }

  // Check safety tabs visibility
  await page.goto(`${BASE}/safety`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(500);
  const tabs = await page.locator('button').all();
  const tabTexts = [];
  for (const t of tabs.slice(0, 15)) {
    tabTexts.push((await t.textContent()).trim().replace(/\s+/g, ' ').slice(0, 30));
  }
  console.log(`\nSafety tabs: ${tabTexts.join(' | ')}`);

  // Check if ingredientSearch is visible after clicking Ingredients tab
  const ingTab = page.locator('button:has-text("Ingredients")').first();
  if (await ingTab.count() > 0) {
    await ingTab.click();
    await page.waitForTimeout(500);
    const searchVisible = await page.locator('input[x-model="ingredientSearch"]').isVisible();
    console.log(`  ingredientSearch visible after Ingredients tab click: ${searchVisible}`);
  }

  await browser.close();
}
main().catch(console.error);
