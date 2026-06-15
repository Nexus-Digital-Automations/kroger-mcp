/**
 * Shopping list: the page loads authed, and a __E2E__ item can be added via API,
 * seen in the list payload + UI, then removed — verifying it disappears from both.
 */
import { test, expect, E2E_PREFIX } from './fixtures';

test('/shopping-list page loads authed', async ({ authedPage }) => {
  const resp = await authedPage.goto('/shopping-list');
  expect(resp?.ok(), `status ${resp?.status()}`).toBeTruthy();
});

test('add → list → delete a __E2E__ shopping-list item, verify in UI', async ({ authedPage, testUser }) => {
  const suffix = Math.random().toString(36).slice(2, 8);
  const name = `${E2E_PREFIX(testUser.runId)}item-${suffix}`;

  const added = await authedPage.request.post('/api/shopping-list/items', {
    data: { name, quantity: 2 },
  });
  expect(added.ok(), `add returned ${added.status()}`).toBeTruthy();
  const addBody = await added.json();
  const itemId = addBody.item_id || addBody.id;
  expect(itemId, `no item id in response: ${JSON.stringify(addBody)}`).toBeTruthy();

  const listed = await authedPage.request.get('/api/shopping-list');
  expect(listed.ok()).toBeTruthy();
  const listBody = await listed.json();
  const items = Array.isArray(listBody) ? listBody : listBody.items || listBody.data || [];
  expect(items.some((i) => i.name === name)).toBeTruthy();

  await authedPage.goto('/shopping-list');
  await expect(authedPage.locator('body')).toContainText(name);

  const removed = await authedPage.request.delete(`/api/shopping-list/${itemId}`);
  expect(removed.ok(), `delete returned ${removed.status()}`).toBeTruthy();

  await authedPage.goto('/shopping-list');
  await expect(authedPage.getByText(name)).toHaveCount(0);
});
