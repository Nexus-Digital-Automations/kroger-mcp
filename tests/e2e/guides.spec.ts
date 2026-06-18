/**
 * Guides list + create/edit flow.
 *
 * Guides are technique how-tos (a trimmed sibling of recipes). They support
 * full CRUD in the UI, so — unlike recipes.spec — these tests deterministically
 * create a guide rather than skipping on an empty account, then clean it up.
 */
import { test, expect } from './fixtures';

test('guides list page loads and shows nav', async ({ authedPage }) => {
  await authedPage.goto('/guides');
  await expect(authedPage.locator('body')).toContainText(/guide/i, { timeout: 8_000 });
});

test('create a guide, add a step, save, and see it on the detail page', async ({ authedPage }) => {
  await authedPage.goto('/guides');

  // "New guide" creates a draft and redirects to /guides/{id}/edit.
  await authedPage.getByRole('button', { name: /new guide/i }).click();
  await authedPage.waitForURL(/\/guides\/[^/]+\/edit/, { timeout: 8_000 });

  const editUrl = authedPage.url();
  const guideId = editUrl.match(/\/guides\/([^/]+)\/edit/)![1];

  // Fill name + one step.
  await authedPage.locator('input[x-model="name"]').fill('E2E Test Guide');
  await authedPage.getByRole('button', { name: /add step/i }).click();
  await authedPage.locator('textarea[x-model="steps[i]"]').first().fill('Rinse thoroughly');

  await authedPage.getByRole('button', { name: /save guide/i }).click();

  // Lands on the read-only detail page showing the saved content.
  await authedPage.waitForURL(new RegExp(`/guides/${guideId}$`), { timeout: 8_000 });
  await expect(authedPage.locator('body')).toContainText('E2E Test Guide', { timeout: 5_000 });
  await expect(authedPage.locator('body')).toContainText('Rinse thoroughly', { timeout: 5_000 });

  // Clean up — guides are global, so don't leave test data behind.
  const del = await authedPage.request.delete(`/api/guides/${guideId}`);
  expect(del.ok()).toBeTruthy();
});
