/**
 * Dedicated Snacks page (/snacks): browse, add, edit gap-days, remove, and
 * "add ticked to shopping list". Seeds via API (same endpoint the page's own
 * "Add Snack" form posts to) and drives the rest through the real UI.
 */
import { test, expect, E2E_PREFIX } from './fixtures';

test('browse, edit gap days, add ticked to shopping list, then remove', async ({ authedPage, testUser }) => {
  const suffix = Math.random().toString(36).slice(2, 8);
  const snackName = `${E2E_PREFIX(testUser.runId)}snackpage-${suffix}`;
  const snackPid = `E2E_SNACKPAGE_${suffix}`;

  // The Snacks list is auto-provisioned; find it by list_type.
  const lists = await (await authedPage.request.get('/api/favorites/lists')).json();
  const arr = Array.isArray(lists) ? lists : lists.lists || [];
  const snacksList = arr.find((l: { list_type?: string }) => l.list_type === 'snacks');
  expect(snacksList, `no snacks list in ${JSON.stringify(arr)}`).toBeTruthy();

  // Seed via the same endpoint the page's own "Add Snack" form posts to.
  // Never-ordered → check_snacks pre-ticks it, so it's checked by default.
  const added = await authedPage.request.post(`/api/favorites/lists/${snacksList.id}/items`, {
    data: { product_id: snackPid, description: snackName, quantity: 1 },
  });
  expect(added.ok(), `add snack ${added.status()}`).toBeTruthy();

  // Browse: the page renders the merged check-up + item metadata.
  await authedPage.goto('/snacks');
  const row = authedPage.getByRole('row', { name: new RegExp(snackName) });
  await expect(row).toBeVisible();

  // Edit typical_gap_days inline; it auto-saves on blur.
  const gapInput = row.getByLabel('Typical gap in days');
  await gapInput.fill('14');
  await gapInput.blur();
  await expect(row.getByText('Saved')).toBeVisible();
  const item = await (await authedPage.request.get(`/api/favorites/lists/${snacksList.id}/items`)).json();
  const savedItem = item.items.find((i: { product_id: string }) => i.product_id === snackPid);
  expect(savedItem.typical_gap_days).toBe(14);

  // Add ticked to shopping list — never-ordered snack is pre-ticked already.
  await authedPage.getByRole('button', { name: /Add ticked to shopping list/i }).click();
  await expect(authedPage.getByText(/Added \d+ snacks? to shopping list/i)).toBeVisible();
  const shoppingList = await (await authedPage.request.get('/api/shopping-list')).json();
  const listItems = Array.isArray(shoppingList) ? shoppingList : shoppingList.items || [];
  expect(listItems.some((i: { name: string }) => i.name === snackName)).toBeTruthy();

  // Remove via the styled confirm dialog, not a native confirm(). Wait for the
  // dialog itself (scoped by its title) before clicking its confirm button —
  // clicking by role alone races with the row's own identically-labelled button.
  await row.getByRole('button', { name: 'Remove' }).click();
  const dialog = authedPage.locator('.ss-modal-card');
  await expect(dialog.getByText(`Remove "${snackName}"?`)).toBeVisible();
  await dialog.getByRole('button', { name: 'Remove', exact: true }).click();
  await expect(authedPage.getByRole('row', { name: new RegExp(snackName) })).toHaveCount(0);

  // Cleanup: shopping-list entry added above (item removal already verified).
  const cleanupList = await (await authedPage.request.get('/api/shopping-list')).json();
  const cleanupItems = Array.isArray(cleanupList) ? cleanupList : cleanupList.items || [];
  const leftover = cleanupItems.find((i: { name: string }) => i.name === snackName);
  if (leftover) {
    await authedPage.request.delete(`/api/shopping-list/${leftover.id}`);
  }
});

test('Snacks nav link is visible and marks /snacks active', async ({ authedPage }) => {
  await authedPage.goto('/snacks');
  const navLink = authedPage.getByRole('link', { name: 'Snacks' });
  await expect(navLink).toBeVisible();
  await expect(navLink).toHaveClass(/active/);
});
