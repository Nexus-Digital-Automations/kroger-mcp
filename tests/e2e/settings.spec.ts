/**
 * Settings: the page loads authed, and the default-servings setting round-trips
 * (POST a new value, read it back, then revert so the run leaves no state change).
 */
import { test, expect } from './fixtures';

test('/settings page loads authed', async ({ authedPage }) => {
  const resp = await authedPage.goto('/settings');
  expect(resp?.ok(), `status ${resp?.status()}`).toBeTruthy();
});

test('servings POST → read back → revert', async ({ authedPage }) => {
  const before = await authedPage.request.get('/api/settings');
  expect(before.ok()).toBeTruthy();
  const original = await before.json();
  expect(typeof original.servings).toBe('number');

  const next = original.servings === 4 ? 6 : 4;
  const posted = await authedPage.request.post('/api/settings/servings', {
    data: { servings: next },
  });
  expect(posted.ok(), `post returned ${posted.status()}`).toBeTruthy();

  const after = await authedPage.request.get('/api/settings');
  expect((await after.json()).servings).toBe(next);

  // Revert so the run leaves the user's servings unchanged.
  const reverted = await authedPage.request.post('/api/settings/servings', {
    data: { servings: original.servings },
  });
  expect(reverted.ok()).toBeTruthy();
});
