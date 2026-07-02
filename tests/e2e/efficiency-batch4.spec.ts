/**
 * Batch 4 frontend: the two flows that dropped full-page reloads.
 *
 *  - Products "view details" now re-runs the search IN PAGE (sets the query +
 *    calls search()) instead of navigating to /products?q=...;
 *  - the meal-plan selector swaps the plan regions + pushes the URL instead of
 *    window.location — the document never reloads (window.__alive sentinel).
 *
 * A third test pins that the auto-deals path still renders (the endpoint gained
 * a Redis cache; the response shape and grid must be unchanged).
 */
import { test, expect } from './fixtures';

test('products: view-details re-searches in place without navigating', async ({ authedPage }) => {
  await authedPage.goto('/products');
  await authedPage.evaluate(() => ((window as any).__alive = true));

  // Mock the search so the test is hermetic (no live Kroger call).
  await authedPage.route('**/api/products/search**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );

  const desc = 'Organic Whole Milk';
  const [req] = await Promise.all([
    authedPage.waitForRequest('**/api/products/search**'),
    authedPage.evaluate((d) => {
      window.dispatchEvent(
        new CustomEvent('action-menu:view-details', { detail: { item: { description: d } } }),
      );
    }, desc),
  ]);

  // The in-page search ran with the product's description...
  expect(req.url()).toContain('q=Organic');
  await expect(authedPage.locator('input[type="text"][x-model="query"]')).toHaveValue(desc);
  // ...and the page never navigated.
  expect(await authedPage.evaluate(() => (window as any).__alive)).toBe(true);
});

test('meal-plan: selector switches plan via region swap + pushState, no reload', async ({
  authedPage,
}) => {
  const prefix = '__E2E__b4__';
  const created: string[] = [];

  async function listPlans(): Promise<any[]> {
    const r = await authedPage.request.get('/api/meal-plan/list');
    if (!r.ok()) return [];
    const data = await r.json();
    return Array.isArray(data) ? data : data.plans || [];
  }

  try {
    // Ensure two distinct plans exist to switch between. Distinct start_dates
    // (not just distinct names) so `ORDER BY start_date DESC` — used by both
    // the API list and the page's own plan-selector query — ranks them the
    // same way regardless of which worker/connection serves each request;
    // a shared start_date leaves same-day ties in implementation-defined
    // order, which isn't guaranteed to agree between the two independent
    // queries.
    const dates = { A: '2026-06-15', B: '2026-06-16' };
    for (const suffix of ['A', 'B'] as const) {
      const r = await authedPage.request.post('/api/meal-plan', {
        data: { name: `${prefix}${suffix}`, start_date: dates[suffix] },
      });
      expect(r.ok()).toBeTruthy();
    }
    const plans = await listPlans();
    const mine = plans.filter((p) => (p.name || '').startsWith(prefix));
    mine.forEach((p) => created.push(p.id));
    expect(mine.length).toBeGreaterThanOrEqual(2);
    const planA = mine.find((p) => p.name === `${prefix}A`);
    const planB = mine.find((p) => p.name === `${prefix}B`);
    expect(planA && planB).toBeTruthy();

    await authedPage.goto(`/meal-plan?plan_id=${planA.id}`);
    await authedPage.evaluate(() => ((window as any).__alive = true));

    await authedPage.locator('select.min-w-48').selectOption(planB.id);

    // URL tracks the new plan, the metadata strip shows it, and no reload fired.
    await expect(authedPage).toHaveURL(new RegExp(`plan_id=${planB.id}`));
    await expect(authedPage.locator('#mealplan-meta')).toContainText(planB.name, {
      timeout: 5_000,
    });
    expect(await authedPage.evaluate(() => (window as any).__alive)).toBe(true);
  } finally {
    for (const id of created) {
      await authedPage.request.delete(`/api/meal-plan/${id}`).catch(() => {});
    }
  }
});

test('products: auto-deals still renders deal cards', async ({ authedPage }) => {
  await authedPage.route('**/api/deals/auto**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          product_id: '0009999990002',
          description: 'Mock Sale Avocados',
          brand: 'MockFarm',
          regular_price: 4.99,
          sale_price: 2.99,
          on_sale: true,
          savings_percent: 40,
        },
      ]),
    }),
  );

  await authedPage.goto('/products');
  await authedPage.getByRole('button', { name: /^deals$/i }).first().click();

  await expect(authedPage.getByText('Mock Sale Avocados')).toBeVisible({ timeout: 5_000 });
});
