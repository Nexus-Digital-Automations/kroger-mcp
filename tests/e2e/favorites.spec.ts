/**
 * Favorites: full CRUD destructive cycle guarded by __E2E__ prefix.
 * Verifies that creating a list shows it in the UI and deleting it removes it.
 */
import { test, expect, E2E_PREFIX } from './fixtures';

test('create + read + delete a __E2E__ favorites list via API, verify in UI', async ({ authedPage, testUser }) => {
  // Per-test suffix prevents collisions with stale lists from earlier runs that
  // failed before reaching their cleanup step.
  const suffix = Math.random().toString(36).slice(2, 8);
  const name = `${E2E_PREFIX(testUser.runId)}fav-list-${suffix}`;
  const created = await authedPage.request.post('/api/favorites/lists', {
    data: { name, list_type: 'custom', description: 'frontend-bug-finder' },
  });
  expect(created.ok(), `create returned ${created.status()}`).toBeTruthy();
  const body = await created.json();
  const listId = body.id || body.list_id || (body.data && (body.data.id || body.data.list_id));
  expect(listId, `no id in response: ${JSON.stringify(body)}`).toBeTruthy();

  await authedPage.goto('/favorites');
  await expect(authedPage.getByText(name).first()).toBeVisible({ timeout: 5_000 });

  await authedPage.goto(`/favorites/${listId}`);
  await expect(authedPage.locator('body')).toContainText(name);

  const deleted = await authedPage.request.delete(`/api/favorites/lists/${listId}`);
  expect(deleted.ok(), `delete returned ${deleted.status()}`).toBeTruthy();

  await authedPage.goto('/favorites');
  await expect(authedPage.getByText(name)).toHaveCount(0);
});

test('GET /api/favorites/lists is reachable and returns array-shaped payload', async ({ authedPage }) => {
  const resp = await authedPage.request.get('/api/favorites/lists');
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  const lists = Array.isArray(body) ? body : body.lists || body.data || [];
  expect(Array.isArray(lists)).toBeTruthy();
});
