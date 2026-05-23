/**
 * Destructive CRUD coverage for the API endpoints reachable from the UI.
 * Every created entity is prefixed with __E2E__{runId}__ so teardown can find and remove it.
 * If an endpoint refuses a payload shape, the failure is recorded so the report flags it.
 */
import { test, expect, E2E_PREFIX } from './fixtures';

test('custom ingredient: create -> appears in list -> delete', async ({ authedPage, testUser }) => {
  const name = `${E2E_PREFIX(testUser.runId)}ing`;
  const created = await authedPage.request.post('/api/ingredients/custom', {
    data: { name, severity: 'warning', reason: 'frontend-bug-finder' },
  });
  expect(created.ok(), `POST returned ${created.status()}: ${await created.text()}`).toBeTruthy();

  const listed = await authedPage.request.get('/api/ingredients/custom');
  expect(listed.ok()).toBeTruthy();
  const body = await listed.json();
  const names = JSON.stringify(body);
  expect(names).toContain(name);

  const deleted = await authedPage.request.delete(`/api/ingredients/custom/${encodeURIComponent(name)}`);
  expect(deleted.ok(), `DELETE returned ${deleted.status()}`).toBeTruthy();
});

test('shopping list: add item -> appears -> delete', async ({ authedPage, testUser }) => {
  const name = `${E2E_PREFIX(testUser.runId)}shop-item`;
  const created = await authedPage.request.post('/api/shopping-list/items', {
    data: { name, quantity: 1, unit: 'ea' },
  });
  expect(created.ok(), `POST returned ${created.status()}: ${await created.text()}`).toBeTruthy();
  const body = await created.json();
  const itemId = body.id || body.item_id || (body.item && body.item.id);
  expect(itemId, `no id in response: ${JSON.stringify(body)}`).toBeTruthy();

  const list = await authedPage.request.get('/api/shopping-list');
  expect(list.ok()).toBeTruthy();
  expect(JSON.stringify(await list.json())).toContain(name);

  const deleted = await authedPage.request.delete(`/api/shopping-list/${itemId}`);
  expect(deleted.ok(), `DELETE returned ${deleted.status()}`).toBeTruthy();
});

test('safety approved list: POST then DELETE round-trip is symmetric', async ({ authedPage, testUser }) => {
  const productId = `__E2E__${testUser.runId}__safety-product`;
  const created = await authedPage.request.post('/api/safety/approved', {
    data: { product_id: productId, description: '__E2E__ approved item', reason: 'bug-finder' },
  });
  expect(created.ok(), `POST returned ${created.status()}: ${await created.text()}`).toBeTruthy();

  const deleted = await authedPage.request.delete(`/api/safety/approved/${productId}`);
  expect(deleted.ok(), `DELETE returned ${deleted.status()}`).toBeTruthy();
});
