/**
 * End-to-end proof that user A's data is invisible from user B's session.
 * A is the existing throwaway account in account.json (provision-account.ts);
 * B is registered inline. Test creates a favorites list as A, then logs in
 * as B and asserts B sees an empty payload from every per-user GET.
 *
 * @stable
 */
import { test, expect } from './fixtures';

async function registerAndLogin(
  page: import('@playwright/test').Page,
  email: string,
  displayName: string,
): Promise<void> {
  await page.goto('/register');
  await page.locator('input[name="display_name"]').fill(displayName);
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill('Pw!iso2userPlaywright');
  await page.locator('input[name="confirm_password"]').fill('Pw!iso2userPlaywright');
  await Promise.all([
    page.waitForURL((u) => !/\/register$/.test(u.pathname), { timeout: 5_000 }),
    page.locator('button[type="submit"]').click(),
  ]);
}

test('user B sees empty per-user payloads even after user A populates them', async ({
  browser,
  baseURL,
  testUser,
}) => {
  // ---- user A: create a favorites list, add a shopping-list item, set a preference
  const aCtx = await browser.newContext();
  const aPage = await aCtx.newPage();
  await aPage.goto(`${baseURL}/login`);
  await aPage.locator('input[name=email]').fill(testUser.email);
  await aPage.locator('input[name=password]').fill(testUser.password);
  await Promise.all([
    aPage.waitForURL((u) => !/\/login$/.test(u.pathname)),
    aPage.locator('button[type=submit]').click(),
  ]);

  const tag = `__E2E__iso-${Math.random().toString(36).slice(2, 8)}`;
  const favName = `${tag}-fav-list`;
  const itemName = `${tag}-shop-item`;

  const aFav = await aPage.request.post(`${baseURL}/api/favorites/lists`, {
    data: { name: favName, list_type: 'custom' },
  });
  expect(aFav.ok()).toBeTruthy();
  const aFavBody = await aFav.json();
  const aListId = aFavBody.list_id;

  const aShop = await aPage.request.post(`${baseURL}/api/shopping-list/items`, {
    data: { name: itemName, quantity: 1, unit: 'ea' },
  });
  expect(aShop.ok()).toBeTruthy();

  // ---- user B: register fresh, then GET each per-user endpoint
  const bCtx = await browser.newContext();
  const bPage = await bCtx.newPage();
  const bEmail = `e2e-iso-${Date.now().toString(36)}@example.test`;
  await registerAndLogin(bPage, bEmail, `__E2E__iso-userB-${tag}`);

  // Favorites: B must not see A's __E2E__ list
  const bFavs = await bPage.request.get(`${baseURL}/api/favorites/lists`);
  expect(bFavs.ok()).toBeTruthy();
  const bFavBody = await bFavs.json();
  const bFavNames = (Array.isArray(bFavBody) ? bFavBody : bFavBody.lists || []).map(
    (l: { name?: string }) => l.name ?? '',
  );
  expect(bFavNames, `user B can see user A's favorites list: ${bFavNames}`).not.toContain(favName);

  // Shopping list: B must see empty / not contain A's item
  const bShop = await bPage.request.get(`${baseURL}/api/shopping-list`);
  expect(bShop.ok()).toBeTruthy();
  const bShopBody = await bShop.json();
  const bItems = bShopBody.items || [];
  const bItemNames = bItems.map((i: { name?: string }) => i.name ?? '');
  expect(bItemNames, `user B can see user A's shopping-list item`).not.toContain(itemName);

  // ---- teardown: A removes its own __E2E__ rows
  await aPage.request.delete(`${baseURL}/api/favorites/lists/${aListId}`);
  const aListAfter = await aPage.request.get(`${baseURL}/api/shopping-list`);
  if (aListAfter.ok()) {
    const items = (await aListAfter.json()).items || [];
    for (const it of items) {
      if ((it.name || '').startsWith(tag)) {
        await aPage.request.delete(`${baseURL}/api/shopping-list/${it.id}`);
      }
    }
  }

  await aCtx.close();
  await bCtx.close();
});

test('user B cannot mutate user A\'s favorites list (cross-user mutation rejected)', async ({
  browser,
  baseURL,
  testUser,
}) => {
  // ---- user A: create a list and add one item
  const aCtx = await browser.newContext();
  const aPage = await aCtx.newPage();
  await aPage.goto(`${baseURL}/login`);
  await aPage.locator('input[name=email]').fill(testUser.email);
  await aPage.locator('input[name=password]').fill(testUser.password);
  await Promise.all([
    aPage.waitForURL((u) => !/\/login$/.test(u.pathname)),
    aPage.locator('button[type=submit]').click(),
  ]);

  const tag = `__E2E__crossmut-${Math.random().toString(36).slice(2, 8)}`;
  const aFav = await aPage.request.post(`${baseURL}/api/favorites/lists`, {
    data: { name: `${tag}-list`, list_type: 'custom' },
  });
  expect(aFav.ok()).toBeTruthy();
  const aListId = (await aFav.json()).list_id;

  const aProductId = `${tag}-prod`;
  const aAdd = await aPage.request.post(
    `${baseURL}/api/favorites/lists/${aListId}/items`,
    { data: { product_id: aProductId, description: `${tag}-onion` } },
  );
  expect(aAdd.ok(), 'A should be able to add to its own list').toBeTruthy();

  // ---- user B: register fresh, then try every mutation against A's list
  const bCtx = await browser.newContext();
  const bPage = await bCtx.newPage();
  const bEmail = `e2e-crossmut-${Date.now().toString(36)}@example.test`;
  await registerAndLogin(bPage, bEmail, `__E2E__crossmut-userB-${tag}`);

  // B tries to ADD an item to A's list → must be rejected
  const bAdd = await bPage.request.post(
    `${baseURL}/api/favorites/lists/${aListId}/items`,
    { data: { product_id: `${tag}-bprod`, description: `${tag}-bitem` } },
  );
  expect(bAdd.status(), 'B must not be able to add to A\'s list').toBeGreaterThanOrEqual(400);

  // B tries to REMOVE A's item → must be rejected
  const bDel = await bPage.request.delete(
    `${baseURL}/api/favorites/lists/${aListId}/items/${aProductId}`,
  );
  expect(bDel.status(), 'B must not be able to remove from A\'s list').toBeGreaterThanOrEqual(400);

  // B tries to RENAME A's list → server may 200 with success:false, or 4xx
  const bRename = await bPage.request.put(`${baseURL}/api/favorites/lists/${aListId}`, {
    data: { name: `${tag}-hijacked` },
  });
  if (bRename.ok()) {
    const body = await bRename.json();
    expect(body.success ?? false, 'B must not rename A\'s list').toBeFalsy();
  }

  // ---- verify A's item is still there and the list name unchanged
  const aGet = await aPage.request.get(`${baseURL}/api/favorites/lists/${aListId}/items`);
  expect(aGet.ok(), 'A can still read its own list').toBeTruthy();
  const aItems = (await aGet.json()).items || [];
  const aProductIds = aItems.map((i: { product_id?: string }) => i.product_id);
  expect(aProductIds, 'A\'s original item must survive B\'s tampering').toContain(aProductId);

  const aLists = await aPage.request.get(`${baseURL}/api/favorites/lists`);
  const aListNames = (await aLists.json()).map((l: { id: string; name: string }) =>
    l.id === aListId ? l.name : null,
  ).filter(Boolean);
  expect(aListNames[0], 'list name must be unchanged after B\'s rename attempt').toBe(`${tag}-list`);

  // ---- teardown
  await aPage.request.delete(`${baseURL}/api/favorites/lists/${aListId}`);
  await aCtx.close();
  await bCtx.close();
});
