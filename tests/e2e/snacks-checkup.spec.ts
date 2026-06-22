/**
 * Snack check-up: a never-ordered snack on the built-in Snacks list is surfaced
 * (pre-ticked) as a check-up step inside the "Send to Kroger Cart" modal, BEFORE
 * the add/skip/manual preview. Seeds via API, drives the modal in the UI.
 */
import { test, expect, E2E_PREFIX } from './fixtures';

test('snack check-up step appears before the cart preview', async ({ authedPage, testUser }) => {
  const suffix = Math.random().toString(36).slice(2, 8);
  const snackName = `${E2E_PREFIX(testUser.runId)}snack-${suffix}`;
  const snackPid = `E2E_SNACK_${suffix}`;
  const itemName = `${E2E_PREFIX(testUser.runId)}item-${suffix}`;

  // The Snacks list is auto-provisioned; find it by list_type.
  const lists = await (await authedPage.request.get('/api/favorites/lists')).json();
  const arr = Array.isArray(lists) ? lists : lists.lists || [];
  const snacksList = arr.find((l: { list_type?: string }) => l.list_type === 'snacks');
  expect(snacksList, `no snacks list in ${JSON.stringify(arr)}`).toBeTruthy();

  // Seed a never-ordered snack → check_snacks pre-ticks it.
  const addSnack = await authedPage.request.post(
    `/api/favorites/lists/${snacksList.id}/items`,
    { data: { product_id: snackPid, description: snackName, quantity: 1 } },
  );
  expect(addSnack.ok(), `add snack ${addSnack.status()}`).toBeTruthy();

  // The Send button only shows when the shopping list is non-empty.
  const addItem = await authedPage.request.post('/api/shopping-list/items', {
    data: { name: itemName, quantity: 1 },
  });
  expect(addItem.ok()).toBeTruthy();
  const itemId = (await addItem.json()).item_id;

  // The check endpoint flags the seeded snack.
  const check = await (await authedPage.request.get('/api/favorites/snacks/check')).json();
  expect(check.candidates.some(
    (c: { product_id: string; pre_ticked: boolean }) => c.product_id === snackPid && c.pre_ticked,
  )).toBeTruthy();

  // Drive the modal: open it, the snack check-up step renders first.
  await authedPage.goto('/shopping-list');
  await authedPage.getByRole('button', { name: /Send to Kroger Cart/i }).click();
  await expect(authedPage.getByText('Snack check-up')).toBeVisible();
  await expect(authedPage.getByText(snackName)).toBeVisible();
  // Footer shows the Continue button (not Confirm) during the snack step.
  await expect(authedPage.getByRole('button', { name: /Continue/i })).toBeVisible();

  // Cleanup.
  await authedPage.request.delete(`/api/favorites/lists/${snacksList.id}/items/${snackPid}`);
  await authedPage.request.delete(`/api/shopping-list/${itemId}`);
});
