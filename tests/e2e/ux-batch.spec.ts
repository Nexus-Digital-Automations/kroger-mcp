/**
 * UX batch: toast migration, pantry product picker, decoupled linking popover,
 * and the recipe→shopping-list→cart one-flow chain.
 *
 * Each test pins a behavior the batch introduced:
 *  - no native alert() dialogs anywhere on the exercised paths;
 *  - the pantry Add-Item modal links products via SEARCH, not raw IDs;
 *  - typing in the linking popover's search box never renames the ingredient;
 *  - the post-schedule toast's "Send to Kroger cart" action opens the shared
 *    cartSend modal and fires the confirm:false preview POST.
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

function failOnNativeDialog(page: import('@playwright/test').Page) {
  page.on('dialog', (d) => {
    // confirm() prompts are still legitimate for destructive actions; native
    // alert() is what the batch eliminated.
    if (d.type() === 'alert') throw new Error(`native alert() fired: ${d.message()}`);
    return d.dismiss();
  });
}

test('pantry restock errors surface as styled toasts, never alert()', async ({ authedPage }) => {
  failOnNativeDialog(authedPage);
  await authedPage.route('**/api/pantry/restock', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
  );
  await authedPage.goto('/pantry');
  await authedPage.getByRole('button', { name: /restock all low/i }).click();
  // Either outcome ("No low items" info or "N of M restocks failed" warn) must
  // render inside the global toast stack.
  const toastStack = authedPage.locator('div[x-data="toastStack"]');
  await expect(toastStack).toContainText(/restock/i, { timeout: 5_000 });
});

test('pantry add modal: search picker fills the POST body, no navigation', async ({
  authedPage,
}) => {
  failOnNativeDialog(authedPage);
  await authedPage.route('**/api/products/search**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([MOCK_PRODUCT]),
    })
  );
  let addBody: any = null;
  await authedPage.route('**/api/pantry/add', async (route) => {
    addBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{"success": true}',
    });
  });

  await authedPage.goto('/pantry');
  // Navigation sentinel: must survive the whole add flow (no page reload).
  await authedPage.evaluate(() => ((window as any).__alive = true));

  await authedPage.getByRole('button', { name: /add item/i }).click();
  const picker = authedPage.getByPlaceholder(/search kroger by name/i);
  await expect(picker).toBeVisible();
  await picker.fill('tomatoes');
  await authedPage.getByRole('button', { name: /mock organic tomatoes/i }).click();
  // Picker selection chip shows the product id.
  await expect(authedPage.getByText(MOCK_PRODUCT.product_id)).toBeVisible();
  await authedPage.getByRole('button', { name: /add to pantry/i }).click();

  await expect.poll(() => addBody, { timeout: 5_000 }).not.toBeNull();
  expect(addBody.product_id).toBe(MOCK_PRODUCT.product_id);
  expect(addBody.description).toMatch(/mock organic tomatoes/i);
  expect(await authedPage.evaluate(() => (window as any).__alive)).toBe(true);
});

test('linking popover: its search box refines results WITHOUT renaming the ingredient', async ({
  authedPage,
  testUser,
}) => {
  failOnNativeDialog(authedPage);
  // Provision a recipe with one manual ingredient via the API.
  const created = await authedPage.request.post('/api/recipes', {
    data: { name: `__E2E__${testUser.runId}__popover`, servings: 2 },
  });
  expect(created.ok()).toBeTruthy();
  const recipeId = (await created.json()).recipe_id || (await created.json()).id;
  const createdJson = recipeId ? { recipe_id: recipeId } : await created.json();
  const rid = createdJson.recipe_id;
  expect(rid).toBeTruthy();
  try {
    const putRes = await authedPage.request.put(`/api/recipes/${rid}/ingredients`, {
      data: {
        ingredients: [
          { name: 'basil', quantity: 1, unit: 'bunch', override: true, override_reason: 'e2e' },
        ],
      },
    });
    expect(putRes.ok()).toBeTruthy();

    await authedPage.route('**/api/products/search**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([MOCK_PRODUCT]),
      })
    );
    let linkPutBody: any = null;
    await authedPage.route(`**/api/recipes/${rid}/ingredients`, async (route) => {
      if (route.request().method() === 'PUT') {
        linkPutBody = route.request().postDataJSON();
      }
      await route.continue();
    });

    await authedPage.goto(`/recipes/${rid}/edit`);
    const nameCell = authedPage.locator('.ing-name').first();
    await expect(nameCell).toHaveText('basil', { timeout: 8_000 });
    await nameCell.click(); // focus opens the popover

    const popoverSearch = authedPage.getByPlaceholder(/search kroger products/i);
    await expect(popoverSearch).toBeVisible({ timeout: 5_000 });

    // THE decoupling assertion: refining the search must not touch the name.
    await popoverSearch.fill('tomato');
    await expect(nameCell).toHaveText('basil');

    // Pick the mocked result → the link PUT carries the product_id.
    await authedPage
      .locator('.ss-autocomplete-row', { hasText: 'Mock Organic Tomatoes' })
      .first()
      .click();
    await expect.poll(() => linkPutBody, { timeout: 8_000 }).not.toBeNull();
    const linked = (linkPutBody.ingredients || []).find(
      (i: any) => i.product_id === MOCK_PRODUCT.product_id
    );
    expect(linked).toBeTruthy();
  } finally {
    await authedPage.request.delete(`/api/recipes/${rid}`).catch(() => {});
  }
});

test('recipe→list chain: "Not now" toast action opens cartSend and fires the preview POST', async ({
  authedPage,
  testUser,
}) => {
  failOnNativeDialog(authedPage);
  const created = await authedPage.request.post('/api/recipes', {
    data: { name: `__E2E__${testUser.runId}__chain`, servings: 2 },
  });
  const rid = (await created.json()).recipe_id;
  expect(rid).toBeTruthy();
  try {
    // Mock the add-recipe preview/commit and the cart-send preview.
    await authedPage.route('**/api/shopping-list/add-recipe', (route) => {
      const body = route.request().postDataJSON();
      const payload = body.confirm
        ? { success: true, message: 'Added 1 item.' }
        : {
            recipe_name: 'Chain Test',
            items_to_add: [
              { product_id: MOCK_PRODUCT.product_id, name: 'Tomatoes', quantity: 1 },
            ],
            manual_purchase: [],
            items_to_skip: [],
          };
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(payload),
      });
    });
    let cartPreviewFired = false;
    await authedPage.route('**/api/shopping-list/add-to-cart', (route) => {
      const body = route.request().postDataJSON();
      if (body.confirm === false) cartPreviewFired = true;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], items_to_skip: [], spices: [], summary: {} }),
      });
    });

    await authedPage.goto('/recipes');
    // Same entry the card action-menu uses (menuAddRecipeToList).
    await authedPage.evaluate(
      (id) => (window as any).Alpine.store('recipePreview').openModal(id, 0),
      rid
    );
    await authedPage.getByRole('button', { name: /^confirm ·/i }).click();

    // Scheduling popup → decline.
    await authedPage.getByRole('button', { name: /not now/i }).click();

    // One actionable toast: Shopping list updated → Send to Kroger cart.
    const toastStack = authedPage.locator('div[x-data="toastStack"]');
    await expect(toastStack).toContainText(/shopping list updated/i, { timeout: 5_000 });
    await toastStack.getByRole('button', { name: /send to kroger cart/i }).click();

    // The shared cartSend modal opens and the preview POST fired.
    await expect(authedPage.getByText('Send to Kroger Cart', { exact: true })).toBeVisible({
      timeout: 5_000,
    });
    await expect.poll(() => cartPreviewFired, { timeout: 5_000 }).toBe(true);
  } finally {
    await authedPage.request.delete(`/api/recipes/${rid}`).catch(() => {});
  }
});
