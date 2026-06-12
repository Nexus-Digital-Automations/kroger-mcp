/**
 * Mobile ergonomics: 44px touch targets on coarse pointers + responsive grids.
 * Runs at iPhone-ish 390×844 with hasTouch so `@media (pointer: coarse)` rules
 * actually apply.
 */
import { test, expect } from './fixtures';

test.use({
  viewport: { width: 390, height: 844 },
  hasTouch: true,
  // Force coarse-pointer media to match (Playwright maps this from isMobile on
  // Chromium; hasTouch alone is not sufficient for the media query).
  isMobile: true,
});

test('dashboard stat cards wrap to 2 columns on a phone', async ({ authedPage }) => {
  await authedPage.goto('/dashboard');
  const grid = authedPage.locator('.grid.grid-cols-2.lg\\:grid-cols-4').first();
  await expect(grid).toBeVisible({ timeout: 8_000 });
  const cols = await grid.evaluate(
    (el) => getComputedStyle(el).gridTemplateColumns.split(' ').length
  );
  expect(cols).toBe(2);
});

test('action-menu trigger meets the 44px touch minimum', async ({ authedPage, testUser }) => {
  // A recipe card guarantees an action-menu trigger exists.
  const created = await authedPage.request.post('/api/recipes', {
    data: { name: `__E2E__${testUser.runId}__touch`, servings: 2 },
  });
  const rid = (await created.json()).recipe_id;
  try {
    await authedPage.goto('/recipes');
    const trigger = authedPage.locator('.action-menu-trigger').first();
    await expect(trigger).toBeVisible({ timeout: 8_000 });
    const box = await trigger.boundingBox();
    expect(box, 'trigger has a bounding box').toBeTruthy();
    expect(box!.height).toBeGreaterThanOrEqual(44);
    expect(box!.width).toBeGreaterThanOrEqual(44);
  } finally {
    if (rid) await authedPage.request.delete(`/api/recipes/${rid}`).catch(() => {});
  }
});

test('pantry table collapses low-value columns on a phone', async ({ authedPage }) => {
  // Seed one pantry item so the table renders.
  const added = await authedPage.request.post('/api/pantry/add', {
    data: { product_id: '0009999990021', description: '__E2E__mobile-pantry', level_percent: 50 },
  });
  expect(added.ok()).toBeTruthy();
  try {
    await authedPage.goto('/pantry');
    const table = authedPage.locator('#pantry-table table');
    await expect(table).toBeVisible({ timeout: 8_000 });
    // Always-on columns stay; hidden md:table-cell columns disappear at 390px.
    await expect(table.locator('th', { hasText: 'Item' })).toBeVisible();
    await expect(table.locator('th', { hasText: 'Status' })).toBeVisible();
    await expect(table.locator('th', { hasText: 'Trend' })).toBeHidden();
    await expect(table.locator('th', { hasText: 'Last Used' })).toBeHidden();
    await expect(table.locator('th', { hasText: 'Expiration' })).toBeHidden();
  } finally {
    await authedPage.request.delete('/api/pantry/0009999990021').catch(() => {});
  }
});

test('shopping-list Recipe column hides on a phone', async ({ authedPage }) => {
  const added = await authedPage.request.post('/api/shopping-list/items', {
    data: { product_id: '0009999990022', name: '__E2E__mobile-sl', quantity: 1, unit: '' },
  });
  expect(added.ok()).toBeTruthy();
  const itemId = (await added.json())?.item?.id;
  try {
    await authedPage.goto('/shopping-list');
    const table = authedPage.locator('table');
    await expect(table).toBeVisible({ timeout: 8_000 });
    await expect(table.locator('th', { hasText: 'Item' })).toBeVisible();
    await expect(table.locator('th', { hasText: 'Recipe' })).toBeHidden();
  } finally {
    await authedPage.request
      .delete(`/api/shopping-list/${itemId || '0009999990022'}`)
      .catch(() => {});
  }
});

test('favorites grid wraps to 1 column on a phone', async ({ authedPage, testUser }) => {
  // Same grid-cols-1 md:grid-cols-2 xl:grid-cols-3 treatment as deals.html.
  const created = await authedPage.request.post('/api/favorites/lists', {
    data: { name: `__E2E__${testUser.runId}__mobile-grid`, description: '' },
  });
  const listId = (await created.json()).list_id;
  try {
    await authedPage.goto('/favorites');
    const grid = authedPage.locator('#fav-lists-grid .grid').first();
    await expect(grid).toBeVisible({ timeout: 8_000 });
    const cols = await grid.evaluate(
      (el) => getComputedStyle(el).gridTemplateColumns.split(' ').length
    );
    expect(cols).toBe(1);
  } finally {
    if (listId) await authedPage.request.delete(`/api/favorites/lists/${listId}`).catch(() => {});
  }
});

test('shopping-list delete button meets the 44px touch minimum', async ({ authedPage }) => {
  // Seed one list item so the table (and its delete button) renders.
  const added = await authedPage.request.post('/api/shopping-list/items', {
    data: { product_id: '0009999990002', name: '__E2E__touch-item', quantity: 1, unit: '' },
  });
  expect(added.ok()).toBeTruthy();
  const itemId = (await added.json())?.item?.id;
  try {
    await authedPage.goto('/shopping-list');
    const del = authedPage.locator('.sl-del-btn').first();
    await expect(del).toBeVisible({ timeout: 8_000 });
    const box = await del.boundingBox();
    expect(box).toBeTruthy();
    expect(box!.height).toBeGreaterThanOrEqual(44);
    expect(box!.width).toBeGreaterThanOrEqual(44);
  } finally {
    // Best-effort cleanup by id or by product id.
    await authedPage.request
      .delete(`/api/shopping-list/${itemId || '0009999990002'}`)
      .catch(() => {});
  }
});
