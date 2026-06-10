/**
 * Deals: /deals redirects into the products page; the auto-deals API is array-shaped
 * (may be empty offline — it needs a live client-credentials token), and the deal
 * watchlist supports a full add → list → delete cycle guarded by __E2E__ prefix.
 * Offline-safe: the watchlist accepts an arbitrary product_id (no live Kroger user token).
 */
import { test, expect, E2E_PREFIX } from './fixtures';

test('GET /deals redirects to the products page', async ({ authedPage }) => {
  await authedPage.goto('/deals');
  await expect(authedPage).toHaveURL(/\/products/);
});

test('GET /api/deals/auto returns an array-shaped payload (empty allowed offline)', async ({ authedPage }) => {
  const resp = await authedPage.request.get('/api/deals/auto?min_savings=5');
  expect(resp.ok(), `status ${resp.status()}`).toBeTruthy();
  const body = await resp.json();
  const deals = Array.isArray(body) ? body : body.deals || body.data || [];
  expect(Array.isArray(deals)).toBeTruthy();
});

test('watchlist add → list → delete a __E2E__ entry', async ({ authedPage, testUser }) => {
  const suffix = Math.random().toString(36).slice(2, 8);
  // Synthetic product_id — the watchlist stores it verbatim, no Kroger validation.
  const productId = `__E2E__${testUser.runId}__deal-${suffix}`;
  const description = `${E2E_PREFIX(testUser.runId)}watch-${suffix}`;

  const added = await authedPage.request.post('/api/deals/watchlist', {
    data: { product_id: productId, description, target_price: 1.99 },
  });
  expect(added.ok(), `add returned ${added.status()}`).toBeTruthy();

  const listed = await authedPage.request.get('/api/deals/watchlist');
  expect(listed.ok()).toBeTruthy();
  const rows = await listed.json();
  const arr = Array.isArray(rows) ? rows : rows.data || [];
  expect(arr.some((r) => r.product_id === productId)).toBeTruthy();

  const removed = await authedPage.request.delete(`/api/deals/watchlist/${encodeURIComponent(productId)}`);
  expect(removed.ok(), `delete returned ${removed.status()}`).toBeTruthy();

  const after = await authedPage.request.get('/api/deals/watchlist');
  const afterBody = await after.json();
  const afterArr = Array.isArray(afterBody) ? afterBody : afterBody.data || [];
  expect(afterArr.some((r) => r.product_id === productId)).toBeFalsy();
});
