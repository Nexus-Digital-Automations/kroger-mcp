/**
 * UX batch 2: favorites no-reload flows + product picker, the ssConfirm styled
 * dialog replacing native confirm()/prompt(), client-side list filters, and
 * per-row double-fire guards.
 *
 * Each test pins a behavior the batch introduced:
 *  - NO native dialog of any kind fires on migrated paths (confirm() is gone);
 *  - favorites create/delete update in place (window.__alive sentinel);
 *  - the favorites-detail Add Item form links products via SEARCH;
 *  - the shopping-list filter narrows visible rows without touching state;
 *  - qty steppers disable while a mutation is in flight (single PATCH).
 */
import { test, expect } from './fixtures';

const MOCK_PRODUCT = {
  product_id: '0009999990001',
  description: 'Mock Organic Tomatoes',
  brand: 'MockFarm',
  size: '16 oz',
  regular_price: 3.49,
  sale_price: null,
  on_sale: false,
  savings_percent: 0,
};

function failOnAnyDialog(page: import('@playwright/test').Page) {
  page.on('dialog', (d) => {
    throw new Error(`native ${d.type()}() fired: ${d.message()}`);
  });
}

/** Delete any favorites lists left behind by earlier aborted runs. */
async function purgeFavoritesLists(page: import('@playwright/test').Page, needle: string) {
  const r = await page.request.get('/api/favorites/lists');
  if (!r.ok()) return;
  const lists = await r.json();
  for (const lst of Array.isArray(lists) ? lists : []) {
    if ((lst.name || '').includes(needle)) {
      await page.request.delete(`/api/favorites/lists/${lst.id}`).catch(() => {});
    }
  }
}

test('favorites: create list via modal → toast + in-place refresh, no reload', async ({
  authedPage,
  testUser,
}) => {
  failOnAnyDialog(authedPage);
  await purgeFavoritesLists(authedPage, 'uxb2');
  const listName = `__E2E__${testUser.runId}__uxb2-create`;
  await authedPage.goto('/favorites');
  await authedPage.evaluate(() => ((window as any).__alive = true));
  try {
    await authedPage.getByRole('button', { name: /new list/i }).click();
    await authedPage.getByPlaceholder(/weekly staples/i).fill(listName);
    await authedPage.getByRole('button', { name: /create list/i }).click();

    const toastStack = authedPage.locator('div[x-data="toastStack"]');
    await expect(toastStack).toContainText(/list created/i, { timeout: 5_000 });
    // The swapped grid contains the new card and the page never navigated.
    await expect(
      authedPage.locator('#fav-lists-grid').getByText(listName)
    ).toBeVisible({ timeout: 5_000 });
    expect(await authedPage.evaluate(() => (window as any).__alive)).toBe(true);
  } finally {
    // Cleanup by name lookup (id only known to the server).
    const lists = await (await authedPage.request.get('/api/favorites/lists')).json();
    const mine = (Array.isArray(lists) ? lists : lists.lists || []).find(
      (l: any) => l.name === listName
    );
    if (mine) await authedPage.request.delete(`/api/favorites/lists/${mine.id}`).catch(() => {});
  }
});

test('favorites: delete asks via styled ssConfirm — cancel keeps, confirm removes in place', async ({
  authedPage,
  testUser,
}) => {
  failOnAnyDialog(authedPage);
  const listName = `__E2E__${testUser.runId}__uxb2-del`;
  const created = await authedPage.request.post('/api/favorites/lists', {
    data: { name: listName, description: '' },
  });
  expect(created.ok()).toBeTruthy();
  const listId = (await created.json()).list_id;

  try {
    await authedPage.goto('/favorites');
    await authedPage.evaluate(() => ((window as any).__alive = true));
    const card = authedPage.locator('.ss-card', { hasText: listName }).first();
    await expect(card).toBeVisible({ timeout: 8_000 });

    // Cancel path: dialog opens, Cancel leaves the list alone.
    await card.locator('button[title="Delete list"]').click();
    const dialog = authedPage.locator('.ss-modal-overlay', { hasText: /delete/i }).first();
    await expect(dialog).toBeVisible({ timeout: 5_000 });
    await dialog.getByRole('button', { name: /cancel/i }).click();
    await expect(card).toBeVisible();

    // Confirm path: list disappears via the in-place swap (no navigation).
    await card.locator('button[title="Delete list"]').click();
    await expect(dialog).toBeVisible({ timeout: 5_000 });
    await dialog.getByRole('button', { name: /delete list/i }).click();
    await expect(
      authedPage.locator('#fav-lists-grid').getByText(listName)
    ).toHaveCount(0, { timeout: 8_000 });
    expect(await authedPage.evaluate(() => (window as any).__alive)).toBe(true);
  } finally {
    if (listId) await authedPage.request.delete(`/api/favorites/lists/${listId}`).catch(() => {});
  }
});

test('favorites detail: Add Item uses the product picker and POSTs the picked id', async ({
  authedPage,
  testUser,
}) => {
  failOnAnyDialog(authedPage);
  await purgeFavoritesLists(authedPage, 'uxb2');
  const listName = `__E2E__${testUser.runId}__uxb2-picker`;
  const created = await authedPage.request.post('/api/favorites/lists', {
    data: { name: listName, description: '' },
  });
  const listId = (await created.json()).list_id;
  expect(listId).toBeTruthy();

  try {
    await authedPage.route('**/api/products/search**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([MOCK_PRODUCT]),
      })
    );
    let addBody: any = null;
    await authedPage.route(`**/api/favorites/lists/${listId}/items`, async (route) => {
      if (route.request().method() === 'POST') {
        addBody = route.request().postDataJSON();
      }
      await route.continue();
    });

    await authedPage.goto(`/favorites/${listId}`);
    await authedPage.evaluate(() => ((window as any).__alive = true));

    const picker = authedPage.getByPlaceholder(/search kroger by name/i);
    await expect(picker).toBeVisible({ timeout: 8_000 });
    await picker.fill('tomatoes');
    await authedPage.getByRole('button', { name: /mock organic tomatoes/i }).click();
    // Selection chip shows the picked product id; description autofilled.
    await expect(authedPage.getByText(MOCK_PRODUCT.product_id)).toBeVisible();
    await authedPage.getByRole('button', { name: /^add$/i }).click();

    await expect.poll(() => addBody, { timeout: 8_000 }).not.toBeNull();
    expect(addBody.product_id).toBe(MOCK_PRODUCT.product_id);
    expect(addBody.description).toMatch(/mock organic tomatoes/i);
    expect(await authedPage.evaluate(() => (window as any).__alive)).toBe(true);
  } finally {
    if (listId) await authedPage.request.delete(`/api/favorites/lists/${listId}`).catch(() => {});
  }
});

async function purgeShoppingListItems(
  page: import('@playwright/test').Page,
  needle: string
) {
  const r = await page.request.get('/api/shopping-list');
  if (!r.ok()) return;
  const d = await r.json();
  for (const item of d.items || []) {
    const name = item.name || item.ingredient_name || '';
    if (name.includes(needle)) {
      await page.request.delete(`/api/shopping-list/${item.id || item.product_id}`).catch(() => {});
    }
  }
}

test('shopping list: filter narrows rows; qty stepper guards against double-fire', async ({
  authedPage,
}) => {
  failOnAnyDialog(authedPage);
  // Purge leftovers from any earlier aborted run, then seed two items.
  await purgeShoppingListItems(authedPage, '__E2E__uxb2');
  const a = await authedPage.request.post('/api/shopping-list/items', {
    data: { product_id: '0009999990011', name: '__E2E__uxb2 milk', quantity: 1, unit: '' },
  });
  const b = await authedPage.request.post('/api/shopping-list/items', {
    data: { product_id: '0009999990012', name: '__E2E__uxb2 bread', quantity: 1, unit: '' },
  });
  expect(a.ok() && b.ok()).toBeTruthy();
  const idA = (await a.json())?.item_id;
  const idB = (await b.json())?.item_id;

  try {
    await authedPage.goto('/shopping-list');
    const rows = authedPage.locator('tbody tr');
    await expect(rows.filter({ hasText: '__E2E__uxb2 milk' })).toHaveCount(1, { timeout: 8_000 });

    // Filter narrows to the matching row only.
    await authedPage.getByPlaceholder(/filter items/i).fill('uxb2 milk');
    await expect(rows.filter({ hasText: '__E2E__uxb2 milk' })).toHaveCount(1);
    await expect(rows.filter({ hasText: '__E2E__uxb2 bread' })).toHaveCount(0);
    await authedPage.getByPlaceholder(/filter items/i).fill('');

    // Double-fire guard: slow the PATCH down, click +, button must disable and
    // exactly ONE request goes out.
    let patchCount = 0;
    await authedPage.route(`**/api/shopping-list/${idA}`, async (route) => {
      if (route.request().method() === 'PATCH') {
        patchCount += 1;
        await new Promise((r) => setTimeout(r, 600));
      }
      await route.continue();
    });
    const milkRow = rows.filter({ hasText: '__E2E__uxb2 milk' }).first();
    const plus = milkRow.getByRole('button', { name: '+', exact: true });
    await plus.click();
    await expect(plus).toBeDisabled();
    await expect(plus).toBeEnabled({ timeout: 8_000 });
    expect(patchCount).toBe(1);
  } finally {
    if (idA) await authedPage.request.delete(`/api/shopping-list/${idA}`).catch(() => {});
    if (idB) await authedPage.request.delete(`/api/shopping-list/${idB}`).catch(() => {});
  }
});

test('settings: disconnect-Kroger confirm is the styled dialog, not native', async ({
  authedPage,
}) => {
  failOnAnyDialog(authedPage);
  await authedPage.goto('/settings');
  const disconnect = authedPage.getByRole('button', { name: /disconnect/i }).first();
  // Only authenticated-to-Kroger accounts show the button; skip cleanly if absent.
  if (!(await disconnect.isVisible().catch(() => false))) {
    test.skip(true, 'account not connected to Kroger — confirm path covered by favorites test');
  }
  await disconnect.click();
  const dialog = authedPage.locator('.ss-modal-overlay', { hasText: /disconnect from kroger/i });
  await expect(dialog).toBeVisible({ timeout: 5_000 });
  await dialog.getByRole('button', { name: /cancel/i }).click();
  await expect(dialog).not.toBeVisible();
});
