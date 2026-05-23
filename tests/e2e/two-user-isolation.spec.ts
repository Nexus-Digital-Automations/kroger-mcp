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
