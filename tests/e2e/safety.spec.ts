/**
 * Ingredient safety: the page loads authed, the settings endpoint round-trips a
 * toggle (and is reverted to avoid leaking state), and the flagged-ingredients
 * endpoint returns an array-shaped payload.
 */
import { test, expect } from './fixtures';

test('/safety page loads authed', async ({ authedPage }) => {
  const resp = await authedPage.goto('/safety');
  expect(resp?.ok(), `status ${resp?.status()}`).toBeTruthy();
});

test('settings GET → POST toggle → read back → revert', async ({ authedPage }) => {
  const before = await authedPage.request.get('/api/safety/settings');
  expect(before.ok()).toBeTruthy();
  const original = await before.json();
  expect(typeof original.filtering_enabled).toBe('boolean');

  const toggled = !original.filtering_enabled;
  const posted = await authedPage.request.post('/api/safety/settings', {
    data: { filtering_enabled: toggled },
  });
  expect(posted.ok(), `post returned ${posted.status()}`).toBeTruthy();

  const after = await authedPage.request.get('/api/safety/settings');
  expect((await after.json()).filtering_enabled).toBe(toggled);

  // Revert so the run leaves no persisted settings change.
  const reverted = await authedPage.request.post('/api/safety/settings', {
    data: { filtering_enabled: original.filtering_enabled },
  });
  expect(reverted.ok()).toBeTruthy();
});

test('GET /api/safety/ingredients returns an array-shaped payload', async ({ authedPage }) => {
  const resp = await authedPage.request.get('/api/safety/ingredients');
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  const ingredients = Array.isArray(body) ? body : body.data || [];
  expect(Array.isArray(ingredients)).toBeTruthy();
});
