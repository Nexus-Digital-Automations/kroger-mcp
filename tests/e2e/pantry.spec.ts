/**
 * Pantry: the page loads authed, and a __E2E__ item can be added via API, seen in the
 * pantry UI, then removed by product_id — verifying it disappears from the page.
 * (Pantry has no JSON list endpoint, so this spec deletes its own item by product_id.)
 */
import { test, expect, E2E_PREFIX } from './fixtures';

test('/pantry page loads authed', async ({ authedPage }) => {
  const resp = await authedPage.goto('/pantry');
  expect(resp?.ok(), `status ${resp?.status()}`).toBeTruthy();
});

test('add → reflect in UI → delete a __E2E__ pantry item', async ({ authedPage, testUser }) => {
  const suffix = Math.random().toString(36).slice(2, 8);
  const productId = `__E2E__${testUser.runId}__pantry-${suffix}`;
  const description = `${E2E_PREFIX(testUser.runId)}pantry-${suffix}`;

  const added = await authedPage.request.post('/api/pantry/add', {
    data: { product_id: productId, description, level_percent: 50 },
  });
  expect(added.ok(), `add returned ${added.status()}`).toBeTruthy();

  await authedPage.goto('/pantry');
  await expect(authedPage.locator('body')).toContainText(description);

  const removed = await authedPage.request.delete(`/api/pantry/${encodeURIComponent(productId)}`);
  expect(removed.ok(), `delete returned ${removed.status()}`).toBeTruthy();

  await authedPage.goto('/pantry');
  await expect(authedPage.getByText(description)).toHaveCount(0);
});
