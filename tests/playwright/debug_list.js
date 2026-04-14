// Debug: trace the add-to-list API call
const { chromium } = require('playwright');

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errs = [];
  page.on('console', msg => { if (msg.type() === 'error') errs.push(msg.text()); });

  // Capture all shopping-list requests
  const requests = [];
  page.on('request', req => {
    if (req.url().includes('shopping-list')) {
      requests.push({ url: req.url(), method: req.method(), body: req.postData() });
    }
  });
  const responses = [];
  page.on('response', async res => {
    if (res.url().includes('shopping-list')) {
      let body;
      try { body = await res.text(); } catch { body = '?'; }
      responses.push({ url: res.url(), status: res.status(), body: body.slice(0, 200) });
    }
  });

  await page.goto('http://127.0.0.1:8000/recipes', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Find first "List" button and check its containing card
  const firstListBtn = page.locator('button:has-text("List")').first();
  const btnBox = await firstListBtn.boundingBox();
  console.log('First List button position:', btnBox);
  const btnHtml = await firstListBtn.evaluate(el => el.outerHTML.slice(0, 500));
  console.log('Button HTML:', btnHtml);

  await firstListBtn.click();
  await page.waitForTimeout(2000);

  console.log('\nRequests made:');
  requests.forEach(r => console.log(` - ${r.method} ${r.url}\n   Body: ${r.body}`));
  console.log('\nResponses received:');
  responses.forEach(r => console.log(` - ${r.status} ${r.url}\n   Body: ${r.body}`));

  if (errs.length) console.log('\nJS Errors:', errs);

  await browser.close();
}

main().catch(console.error);
