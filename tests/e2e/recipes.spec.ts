/**
 * Recipes list + detail page interactions.
 * Verifies the inline-edit recipe detail page and ingredient scaling.
 *
 * NOTE: with multi-tenant auth, the throwaway test user starts with zero recipes.
 * Tests gracefully skip when the account has no recipes — running this suite as a
 * data-owning account (jeremyparker) exercises full coverage; running as a fresh
 * account exercises only the "no data" path of each page.
 */
import { test, expect } from './fixtures';

async function firstRecipeHref(page: import('@playwright/test').Page): Promise<string | null> {
  await page.goto('/recipes');
  const link = page.locator('a[href^="/recipes/"]').first();
  if ((await link.count()) === 0) return null;
  if (!(await link.isVisible().catch(() => false))) return null;
  return link.getAttribute('href');
}

test('recipes list page loads (with or without recipes) and shows nav', async ({ authedPage }) => {
  await authedPage.goto('/recipes');
  await expect(authedPage.locator('body')).toContainText(/recipe/i, { timeout: 8_000 });
});

test('recipe detail page renders ingredients + instructions sections', async ({ authedPage }) => {
  const href = await firstRecipeHref(authedPage);
  test.skip(!href, 'account has no recipes — fresh-user state, nothing to open');
  await authedPage.goto(href!);

  await expect(authedPage.locator('body')).toContainText(/ingredient/i, { timeout: 5_000 });
  await expect(authedPage.locator('body')).toContainText(/instruction|step|directions/i, {
    timeout: 5_000,
  });
});

test('recipe detail: scaling servings updates displayed ingredient quantities', async ({
  authedPage,
}) => {
  const href = await firstRecipeHref(authedPage);
  test.skip(!href, 'account has no recipes — fresh-user state, nothing to scale');
  await authedPage.goto(href!);

  const rows = authedPage.locator('[data-ingredient-row]');
  await expect(rows.first()).toBeVisible({ timeout: 8_000 });
  const ingredientCount = await rows.count();
  test.skip(ingredientCount === 0, 'recipe has no ingredients to scale');

  // The increment button sits in the servings stepper pill, immediately after the
  // numeric servings span. Filtering by literal "+" text avoids matching unrelated
  // icons elsewhere on the page.
  const incButton = authedPage
    .locator('button')
    .filter({ hasText: /^\+$/ })
    .first();
  await expect(incButton).toBeVisible({ timeout: 5_000 });

  const before = await rows.allInnerTexts();
  await incButton.click();
  await incButton.click();
  await incButton.click();
  await authedPage.waitForTimeout(500);

  const after = await rows.allInnerTexts();
  expect(after.join('|'), 'ingredient text should change after scaling').not.toEqual(
    before.join('|'),
  );
});
