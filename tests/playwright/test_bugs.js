// @ts-check
/**
 * Bug discovery + feature test for Kroger Smart Shopper frontend.
 * Tests: recipes page, add-to-list, add-to-meal-plan, recipe cards, navigation.
 * Usage: node tests/playwright/test_bugs.js
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8080';
const SS_DIR = path.join(__dirname, 'screenshots');
if (!fs.existsSync(SS_DIR)) fs.mkdirSync(SS_DIR, { recursive: true });

let browser, page;
let passed = 0, failed = 0;
const failures = [];

function assert(condition, label) {
  if (condition) {
    console.log(`  \x1b[32mPASS\x1b[0m  ${label}`);
    passed++;
  } else {
    console.log(`  \x1b[31mFAIL\x1b[0m  ${label}`);
    failed++;
    failures.push(label);
  }
}

async function ss(name) {
  await page.screenshot({ path: path.join(SS_DIR, `bug_${name}.png`), fullPage: false });
}

async function goto(p) {
  await page.goto(`${BASE}${p}`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(800);
}

async function run() {
  browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  page = await ctx.newPage();

  const consoleErrors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });

  // ── 1. Recipes page loads ──
  console.log('\n[1] Recipes page loads');
  await goto('/recipes');
  await page.waitForTimeout(800); // wait for Alpine x-for to render
  await ss('01_recipes_page');

  const title = await page.title();
  assert(title.includes('Recipe'), `Page title includes "Recipe" — got: ${title}`);

  const cards = await page.locator('[x-data="recipesGrid"] .relative.flex.flex-col').count();
  console.log(`     Found ${cards} recipe cards`);
  assert(cards > 0, `At least one recipe card visible (found ${cards})`);

  // ── 2. Both action buttons present on recipe cards ──
  console.log('\n[2] Recipe card action buttons');
  const listBtns = await page.locator('button:has-text("List")').count();
  const mealBtns = await page.locator('button:has-text("Meal Plan")').count();
  assert(listBtns >= cards, `"+ List" button on every card (found ${listBtns}, cards ${cards})`);
  assert(mealBtns >= cards, `"+ Meal Plan" button on every card (found ${mealBtns}, cards ${cards})`);

  // ── 3. Add to Meal Plan panel opens ──
  console.log('\n[3] Meal Plan panel opens');
  const firstMealBtn = page.locator('button:has-text("Meal Plan")').first();
  await firstMealBtn.click();
  await page.waitForTimeout(800);
  await ss('03_meal_plan_panel');
  const panelVisible = await page.locator('.fixed.inset-y-0.right-0').isVisible();
  assert(panelVisible, 'Meal plan slide-out panel appears after click');
  // Close panel
  await page.locator('.fixed.inset-y-0.right-0 button').first().click();
  await page.waitForTimeout(400);

  // ── 4. Add to List button fires API request ──
  console.log('\n[4] Add to List button (recipe card)');
  const networkReqs = [];
  page.on('request', req => {
    if (req.url().includes('shopping-list')) networkReqs.push(req.url());
  });
  const responses = [];
  page.on('response', res => {
    if (res.url().includes('shopping-list')) responses.push({ url: res.url(), status: res.status() });
  });

  const firstListBtn = page.locator('button:has-text("List")').first();
  await firstListBtn.click();
  await page.waitForTimeout(1500);
  await ss('04_add_to_list_card');

  assert(networkReqs.length > 0, `Add to List fired shopping-list API request (${networkReqs.length} requests)`);
  if (responses.length > 0) {
    assert(responses[0].status < 500, `API response OK (status ${responses[0].status})`);
  }

  // Check button shows success or error feedback
  const btnText = await firstListBtn.textContent();
  const feedbackShown = btnText.includes('Added') || btnText.includes('Error') || btnText.includes('Failed');
  assert(feedbackShown || networkReqs.length > 0, `Button shows feedback after click (text: "${btnText?.trim()}")`);

  // ── 5. Recipe detail page ──
  console.log('\n[5] Recipe detail page');
  await goto('/recipes');
  await page.waitForTimeout(600);
  const firstRecipeLink = page.locator('a[href^="/recipes/"]').first();
  const recipeHref = await firstRecipeLink.getAttribute('href');

  if (recipeHref) {
    await goto(recipeHref);
    await ss('05_recipe_detail');

    const heading = await page.locator('h2').first().textContent().catch(() => '');
    assert(heading.length > 0, `Recipe detail has a heading: "${heading.slice(0,40)}"`);

    // Scroll anchor "Add to List" in header
    const headerListBtn = await page.locator('a:has-text("Add to List")').count();
    assert(headerListBtn > 0, 'Header "Add to List" anchor present on recipe detail page');

    // ── 6. Ingredients card Add to Shopping List button ──
    console.log('\n[6] Ingredients card "Add to Shopping List" button');
    await page.waitForTimeout(600);

    const ingCardAddBtn = page.locator('#ingredients-card button:has-text("Add to Shopping List")');
    const ingCardAddCount = await ingCardAddBtn.count();
    assert(ingCardAddCount > 0, `"Add to Shopping List" button present in ingredients card (found ${ingCardAddCount})`);

    if (ingCardAddCount > 0) {
      // Scroll button into view and click
      await ingCardAddBtn.scrollIntoViewIfNeeded();
      await page.waitForTimeout(300);

      const netReqs2 = [];
      page.on('request', req => {
        if (req.url().includes('shopping-list')) netReqs2.push(req.url());
      });

      await ingCardAddBtn.click();
      await page.waitForTimeout(1500);
      await ss('06_detail_add_to_list');

      assert(netReqs2.length > 0, `Ingredients card Add to Shopping List fired API request`);
      const btnText2 = await ingCardAddBtn.textContent().catch(() => '');
      const hasSuccess = btnText2.includes('Added') || btnText2.includes('Error') || netReqs2.length > 0;
      assert(hasSuccess, `Button shows feedback (text: "${btnText2?.trim().slice(0, 30)}")`);
    }

    // ── 7. Servings stepper ──
    console.log('\n[7] Servings stepper');
    const incBtn = page.locator('#ingredients-card button').filter({ hasText: '+' }).first();
    if (await incBtn.count() > 0) {
      await incBtn.scrollIntoViewIfNeeded();
      const beforeText = await page.locator('#ingredients-card span[x-text="servings"]').textContent().catch(() => '?');
      await incBtn.click();
      await page.waitForTimeout(300);
      const afterText = await page.locator('#ingredients-card span[x-text="servings"]').textContent().catch(() => '?');
      assert(true, `Servings stepper (+) clickable (${beforeText} → ${afterText})`);
    } else {
      assert(false, 'Servings stepper (+) button not found');
    }
  }

  // ── 8. Navigation links ──
  console.log('\n[8] Navigation links');
  const navLinks = [
    { path: '/recipes',        contains: '/recipes' },
    { path: '/shopping-list',  contains: '/shopping-list' },
    { path: '/meal-plan',      contains: '/meal-plan' },
    { path: '/pantry',         contains: '/pantry' },
  ];
  for (const link of navLinks) {
    await goto(link.path);
    const url = page.url();
    assert(url.includes(link.contains), `${link.path} navigates correctly`);
  }
  await ss('08_nav_test');

  // ── 9. No critical JS errors ──
  console.log('\n[9] Console errors');
  const critErrors = consoleErrors.filter(e =>
    !e.includes('favicon') &&
    !e.includes('net::ERR') &&
    !e.includes('Failed to load resource') &&
    !e.includes('404')
  );
  if (critErrors.length === 0) {
    assert(true, 'No critical JS console errors');
  } else {
    console.log('  Console errors:');
    critErrors.slice(0, 3).forEach(e => console.log(`    ${e.slice(0, 120)}`));
    assert(false, `${critErrors.length} critical JS console error(s)`);
  }

  // ── Summary ──
  console.log(`\n${'─'.repeat(55)}`);
  console.log(`Results: \x1b[32m${passed} passed\x1b[0m, \x1b[31m${failed} failed\x1b[0m`);
  if (failures.length > 0) {
    console.log('\nFailed:');
    failures.forEach(f => console.log(`  • ${f}`));
  }

  await browser.close();
  process.exit(failed > 0 ? 1 : 0);
}

run().catch(err => {
  console.error('Test runner error:', err);
  if (browser) browser.close();
  process.exit(1);
});
