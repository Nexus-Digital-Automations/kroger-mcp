/**
 * First-run onboarding banner: a brand-new account (0 recipes / 0 meal plans /
 * 0 favorites) sees the "Start here" card on the dashboard; dismissing it
 * persists via localStorage. Registers its own fresh user because the shared
 * suite account accumulates data.
 */
import { test as base, expect } from '@playwright/test';

const test = base;

function freshIdentity() {
  const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  return {
    email: `e2e-onboard-${id}@example.test`,
    password: `Pw!E2E${id}`,
    displayName: `__E2E__${id}__onboard`,
  };
}

test('fresh user sees the Start-here card; dismissal persists across reloads', async ({
  browser,
  baseURL,
}) => {
  const u = freshIdentity();
  const ctx = await browser.newContext();
  const page = await ctx.newPage();

  // Register inline (same flow as scripts/provision-account.ts).
  await page.goto(`${baseURL}/register`);
  await page.locator('input[name="display_name"]').fill(u.displayName);
  await page.locator('input[name="email"]').fill(u.email);
  await page.locator('input[name="password"]').fill(u.password);
  await page.locator('input[name="confirm_password"]').fill(u.password);
  await Promise.all([
    page.waitForURL((url) => !/\/register$/.test(url.pathname), { timeout: 8_000 }),
    page.locator('button[type="submit"]').click(),
  ]);
  // Silence the consent gate so it can't cover the banner.
  await ctx.request.post(`${baseURL}/api/settings/consent`, { data: { updates: {} } }).catch(() => {});

  await page.goto(`${baseURL}/dashboard`);
  const banner = page.locator('#onboarding-banner');
  await expect(banner).toBeVisible({ timeout: 8_000 });
  await expect(banner).toContainText(/add your first recipe/i);

  // Dismiss → hidden now AND after reload (localStorage flag).
  await banner.getByRole('button', { name: /dismiss/i }).click();
  await expect(banner).not.toBeVisible();
  await page.reload();
  await expect(page.locator('#onboarding-banner')).not.toBeVisible({ timeout: 8_000 });

  await ctx.close();
});
