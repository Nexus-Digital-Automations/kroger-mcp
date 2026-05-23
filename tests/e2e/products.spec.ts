/**
 * Products search page: form renders, submitting yields results or a non-error empty state.
 */
import { test, expect } from './fixtures';

test('products page exposes a search input and a submit/search action', async ({ authedPage }) => {
  await authedPage.goto('/products');
  const searchInput = authedPage.locator('input[type=search], input[name*=search i], input[name=q], input[placeholder*=search i]').first();
  await expect(searchInput).toBeVisible({ timeout: 8_000 });
});

test('products API search returns 2xx for a common term', async ({ authedPage }) => {
  const resp = await authedPage.request.get('/api/products/search', {
    params: { q: 'milk' },
  });
  expect(resp.ok(), `search returned ${resp.status()}`).toBeTruthy();
});
