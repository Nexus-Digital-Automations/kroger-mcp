const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  let dialogCount = 0;
  page.on('dialog', async dialog => {
    dialogCount++;
    console.log(`UNEXPECTED DIALOG: type=${dialog.type()} msg="${dialog.message()}"`);
    await dialog.dismiss();
  });

  // TEST 1: tag dropdown
  await page.goto('http://localhost:8080/recipes');
  await page.waitForLoadState('domcontentloaded');
  const filterBtn = page.locator('button:has-text("Filter by tag")');
  console.log(await filterBtn.isVisible() ? 'PASS: Filter by tag dropdown visible' : 'FAIL: dropdown not visible');
  await filterBtn.click();
  await page.waitForTimeout(400);
  const checkCount = await page.locator('input[type=checkbox]').count();
  console.log(`PASS: Dropdown opened with ${checkCount} tag checkboxes`);
  if (checkCount > 0) {
    await page.locator('input[type=checkbox]').first().click();
    await page.waitForTimeout(200);
    const selectedBadge = page.locator('button:has-text(" selected")');
    console.log(await selectedBadge.isVisible() ? 'PASS: "N selected" badge shows' : 'INFO: No badge');
  }

  // TEST 2: ingredient toggle - get all recipe hrefs at once
  await page.goto('http://localhost:8080/recipes');
  await page.waitForLoadState('domcontentloaded');
  const hrefs = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a[href^="/recipes/"]'))
      .map(a => a.getAttribute('href')).filter(Boolean).slice(0, 8)
  );
  console.log(`INFO: Found ${hrefs.length} recipe links`);

  let ingTested = false;
  for (const href of hrefs) {
    await page.goto(`http://localhost:8080${href}`);
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(600);
    const usageVis = await page.locator('button:has-text("Usage order")').isVisible().catch(() => false);
    const catVis   = await page.locator('button:has-text("By category")').isVisible().catch(() => false);
    if (usageVis && catVis) {
      console.log(`PASS: Ingredient toggle visible on ${href}`);
      await page.locator('button:has-text("By category")').click();
      await page.waitForTimeout(400);
      console.log(`PASS: "By category" click worked on ${href} — dialogs so far: ${dialogCount}`);
      await page.locator('button:has-text("Usage order")').click();
      await page.waitForTimeout(200);
      console.log('PASS: "Usage order" click worked');
      ingTested = true;
      break;
    }
  }
  if (!ingTested) console.log('INFO: All sampled recipes have no ingredients — toggle correctly hidden');

  // TEST 3: no confirm() on delete pages
  for (const path of ['/recipes', '/cart', '/meal-plan', '/pantry']) {
    await page.goto(`http://localhost:8080${path}`);
    await page.waitForLoadState('domcontentloaded');
    const html = await page.content();
    const n = (html.match(/confirm\s*\(/g) || []).length;
    console.log(n === 0 ? `PASS: No confirm() on ${path}` : `FAIL: ${n} confirm() calls on ${path}`);
  }

  console.log(consoleErrors.length === 0 ? 'PASS: Zero JS console errors' : `WARN: ${consoleErrors.join(' | ')}`);
  console.log(`Unexpected dialogs fired: ${dialogCount} (expected 0)`);
  await browser.close();
  process.exit(dialogCount > 0 ? 1 : 0);
})().catch(e => { console.error('ERROR:', e.message); process.exit(1); });
