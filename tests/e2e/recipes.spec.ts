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

test('recipe edit: servings stepper increments the (persisted) base servings', async ({
  authedPage,
}) => {
  const href = await firstRecipeHref(authedPage);
  test.skip(!href, 'account has no recipes — fresh-user state, nothing to scale');
  // The a00745e redesign split the recipe into /recipes/{id} (read-only view,
  // initial_editing=false) and /recipes/{id}/edit (initial_editing=true). The
  // servings ± stepper is edit-mode-only and now edits the recipe's PERSISTED
  // base servings (PATCH) — there is no separate view-time scaler, so bumping
  // it changes the servings count rather than re-scaling ingredient text.
  const editHref = href!.split('?')[0].replace(/\/+$/, '') + '/edit';
  await authedPage.goto(editHref);

  await expect(authedPage.locator('[data-ingredient-row]').first()).toBeVisible({ timeout: 8_000 });

  // The numeric servings span (x-text="baseServings") sits between the −/+ buttons.
  const incButton = authedPage.getByRole('button', { name: 'Increase servings' });
  await expect(incButton).toBeVisible({ timeout: 5_000 });
  const servings = authedPage.locator('span[x-text="baseServings"]');

  const before = Number((await servings.innerText()).trim());
  await incButton.click();
  await expect(servings).toHaveText(String(before + 1), { timeout: 5_000 });
  await incButton.click();
  await expect(servings).toHaveText(String(before + 2), { timeout: 5_000 });
});
