/**
 * Auth flows: form validation paths + happy-path login with the throwaway account.
 * Critical path — incorrect handling here lets unauthorized users into the dashboard.
 */
import { test, expect } from './fixtures';

test.describe('register form', () => {
  test('rejects mismatched passwords', async ({ page, testUser }) => {
    await page.goto('/register');
    await page.locator('input[name=display_name]').fill('__E2E__mismatch');
    await page.locator('input[name=email]').fill(`reject-${testUser.runId}@example.test`);
    await page.locator('input[name=password]').fill('passw0rd123');
    await page.locator('input[name=confirm_password]').fill('different123');
    await page.locator('button[type=submit]').click();
    await expect(page.locator('.error-msg, [class*=error]').first()).toContainText(/match/i);
  });

  test('rejects passwords under 8 chars', async ({ page, testUser }) => {
    await page.goto('/register');
    await page.locator('input[name=display_name]').fill('__E2E__short');
    await page.locator('input[name=email]').fill(`short-${testUser.runId}@example.test`);
    await page.locator('input[name=password]').fill('short');
    await page.locator('input[name=confirm_password]').fill('short');
    await page.locator('button[type=submit]').click();
    await expect(page.locator('.error-msg, [class*=error]').first()).toContainText(/8 characters/i);
  });

  test('rejects duplicate email', async ({ page, testUser }) => {
    await page.goto('/register');
    await page.locator('input[name=display_name]').fill('__E2E__dup');
    await page.locator('input[name=email]').fill(testUser.email);
    await page.locator('input[name=password]').fill('anotherPw1');
    await page.locator('input[name=confirm_password]').fill('anotherPw1');
    await page.locator('button[type=submit]').click();
    await expect(page.locator('.error-msg, [class*=error]').first()).toContainText(/already exists/i);
  });
});

test.describe('login form', () => {
  test('rejects wrong password with 401 and error message', async ({ page, testUser }) => {
    await page.goto('/login');
    await page.locator('input[name=email]').fill(testUser.email);
    await page.locator('input[name=password]').fill('definitely-wrong-pw');
    const [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/login') && r.request().method() === 'POST'),
      page.locator('button[type=submit]').click(),
    ]);
    expect(resp.status()).toBe(401);
    await expect(page.locator('.error-msg, [class*=error]').first()).toContainText(/invalid/i);
  });

  test('rejects unknown email with 401', async ({ page, testUser }) => {
    await page.goto('/login');
    await page.locator('input[name=email]').fill(`nobody-${testUser.runId}@example.test`);
    await page.locator('input[name=password]').fill('whatever123');
    const [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/login') && r.request().method() === 'POST'),
      page.locator('button[type=submit]').click(),
    ]);
    expect(resp.status()).toBe(401);
  });

  test('happy path: throwaway account can sign in and reach dashboard', async ({ page, testUser }) => {
    await page.goto('/login');
    await page.locator('input[name=email]').fill(testUser.email);
    await page.locator('input[name=password]').fill(testUser.password);
    await Promise.all([
      page.waitForURL((u) => !/\/login$/.test(u.pathname)),
      page.locator('button[type=submit]').click(),
    ]);
    expect(page.url()).toMatch(/\/(dashboard)?$/);
  });
});
