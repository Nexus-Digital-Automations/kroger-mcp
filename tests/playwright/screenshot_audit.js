const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8000';
const SS_DIR = path.join(__dirname, 'screenshots');
if (!fs.existsSync(SS_DIR)) fs.mkdirSync(SS_DIR, { recursive: true });

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  const pages = [
    { path: '/dashboard', name: 'audit_dashboard' },
    { path: '/recipes', name: 'audit_recipes' },
    { path: '/shopping-list', name: 'audit_shopping_list' },
    { path: '/meal-plan', name: 'audit_meal_plan' },
    { path: '/pantry', name: 'audit_pantry' },
    { path: '/products', name: 'audit_products' },
    { path: '/analytics', name: 'audit_analytics' },
    { path: '/settings', name: 'audit_settings' },
    { path: '/favorites', name: 'audit_favorites' },
    { path: '/safety', name: 'audit_safety' },
  ];

  for (const p of pages) {
    await page.goto(`${BASE}${p.path}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1000);
    await page.screenshot({
      path: path.join(SS_DIR, `${p.name}.png`),
      fullPage: true,
    });
    console.log(`  Captured ${p.name}`);
  }

  // Also get a recipe detail page
  await page.goto(`${BASE}/recipes`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(600);
  const href = await page.locator('a[href^="/recipes/"]').first().getAttribute('href');
  if (href) {
    await page.goto(`${BASE}${href}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    await page.screenshot({
      path: path.join(SS_DIR, 'audit_recipe_detail.png'),
      fullPage: true,
    });
    console.log('  Captured audit_recipe_detail');
  }

  await browser.close();
  console.log('Done — screenshots in', SS_DIR);
}

main().catch(console.error);
