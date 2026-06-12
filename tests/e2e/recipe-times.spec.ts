/**
 * Step times & total recipe time (specs/recipe-step-times.md).
 *
 * Pins: auto-detected chips render with the ~ prefix and a header total;
 * a manual override (set via the edit-mode chip popover) beats auto, persists
 * across reload, and clears back to auto; recipe cards carry the time chip
 * and the sort menu offers "Quickest".
 */
import { test, expect } from './fixtures';

async function createTimedRecipe(page: import('@playwright/test').Page, name: string) {
  const created = await page.request.post('/api/recipes', {
    data: { name, servings: 2 },
  });
  expect(created.ok()).toBeTruthy();
  const rid = (await created.json()).recipe_id;
  expect(rid).toBeTruthy();
  const put = await page.request.put(`/api/recipes/${rid}/instructions`, {
    data: {
      instructions: [
        'Sauté the onions for 5 minutes.',
        'Simmer the sauce 20 minutes, stirring occasionally.',
        'Let it rest 10 minutes before serving.',
      ],
    },
  });
  expect(put.ok()).toBeTruthy();
  return rid;
}

test('auto-detected step chips and header total render with zero input', async ({
  authedPage,
  testUser,
}) => {
  const rid = await createTimedRecipe(authedPage, `__E2E__${testUser.runId}__times-auto`);
  try {
    await authedPage.goto(`/recipes/${rid}`);
    const chips = authedPage.locator('.ss-step-time-chip');
    await expect(chips.filter({ hasText: '~5 min' })).toBeVisible({ timeout: 8_000 });
    await expect(chips.filter({ hasText: '~20 min' })).toBeVisible();
    // "Let it rest" is hands-off → passive styling class.
    const restChip = chips.filter({ hasText: '~10 min' }).first();
    await expect(restChip).toHaveClass(/is-passive/);
    // Header summary: 25 active + 10 hands-off = 35 total.
    await expect(authedPage.getByText(/35 min total/)).toBeVisible();
    await expect(authedPage.getByText(/25 min active/)).toBeVisible();
  } finally {
    await authedPage.request.delete(`/api/recipes/${rid}`).catch(() => {});
  }
});

test('chip override beats auto, persists across reload, and clears back', async ({
  authedPage,
  testUser,
}) => {
  const rid = await createTimedRecipe(authedPage, `__E2E__${testUser.runId}__times-ovr`);
  try {
    await authedPage.goto(`/recipes/${rid}/edit`); // edit mode → chips clickable
    // Scope to the step row: every row renders its own (hidden) popover, so
    // page-level .first() would match a different row's input.
    const simmerRow = authedPage.locator('.step-row', { hasText: 'Simmer' }).first();
    const simmerChip = simmerRow.locator('.ss-step-time-chip');
    await expect(simmerChip).toHaveText('~20 min', { timeout: 8_000 });
    await simmerChip.click();

    const minutesInput = simmerRow.getByPlaceholder('min');
    await expect(minutesInput).toBeVisible();
    await minutesInput.fill('35');
    await simmerRow.getByRole('button', { name: 'Save', exact: true }).click();

    // Override renders solid (no ~ prefix) and survives a reload.
    const overrideChip = authedPage
      .locator('.ss-step-time-chip')
      .filter({ hasText: /^35 min$/ });
    await expect(overrideChip).toBeVisible({ timeout: 8_000 });
    await authedPage.reload();
    await expect(
      authedPage.locator('.ss-step-time-chip').filter({ hasText: /^35 min$/ })
    ).toBeVisible({ timeout: 8_000 });

    // Clear → back to the auto-detected value.
    const rowAfterReload = authedPage.locator('.step-row', { hasText: 'Simmer' }).first();
    await rowAfterReload.locator('.ss-step-time-chip').click();
    await rowAfterReload.getByRole('button', { name: 'Clear', exact: true }).click();
    await expect(
      authedPage.locator('.ss-step-time-chip').filter({ hasText: '~20 min' })
    ).toBeVisible({ timeout: 8_000 });
  } finally {
    await authedPage.request.delete(`/api/recipes/${rid}`).catch(() => {});
  }
});

test('recipe cards show the time chip and the sort menu offers Quickest', async ({
  authedPage,
  testUser,
}) => {
  const rid = await createTimedRecipe(authedPage, `__E2E__${testUser.runId}__times-card`);
  try {
    await authedPage.goto('/recipes');
    // Narrow the grid to just our recipe via the page search box (the global
    // recipe_picker modal has an identically-labelled hidden input).
    await authedPage.locator('input[x-model="search"]').fill('times-card');
    const chip = authedPage.locator('.ss-step-time-chip').first();
    await expect(chip).toBeVisible({ timeout: 8_000 });
    await expect(chip).toContainText('35 min');
    // Sort options include the new Quickest entry.
    await expect(authedPage.getByText('Quickest')).toBeAttached();
  } finally {
    await authedPage.request.delete(`/api/recipes/${rid}`).catch(() => {});
  }
});
