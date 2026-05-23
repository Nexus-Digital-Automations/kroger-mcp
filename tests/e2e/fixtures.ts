/**
 * Shared Playwright fixtures: authed page (cookie-based session) + test-run identity.
 * The throwaway account is provisioned once by scripts/provision-account.ts before the suite runs.
 * Credentials live in tests/e2e/_discovery/account.json (gitignored).
 */
import { test as base, expect, BrowserContext } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

export interface TestUser {
  email: string;
  password: string;
  displayName: string;
  runId: string;
}

const ACCOUNT_FILE = path.join(__dirname, '_discovery', 'account.json');

function loadTestUser(): TestUser {
  if (!fs.existsSync(ACCOUNT_FILE)) {
    throw new Error(
      `account.json missing. Run: npx tsx tests/e2e/scripts/provision-account.ts`,
    );
  }
  return JSON.parse(fs.readFileSync(ACCOUNT_FILE, 'utf8')) as TestUser;
}

async function loginCookie(ctx: BrowserContext, baseURL: string, u: TestUser): Promise<void> {
  const page = await ctx.newPage();
  await page.goto(`${baseURL}/login`);
  await page.locator('input[name="email"]').fill(u.email);
  await page.locator('input[name="password"]').fill(u.password);
  await Promise.all([page.waitForURL((url) => !/\/login$/.test(url.pathname)), page.locator('button[type="submit"]').click()]);
  await page.close();
}

export const test = base.extend<{ testUser: TestUser; authedPage: import('@playwright/test').Page }>({
  testUser: async ({}, use) => {
    await use(loadTestUser());
  },
  authedPage: async ({ browser, baseURL, testUser }, use) => {
    const ctx = await browser.newContext();
    await loginCookie(ctx, baseURL!, testUser);
    const page = await ctx.newPage();
    await use(page);
    await ctx.close();
  },
});

export { expect };
export const E2E_PREFIX = (runId: string) => `__E2E__${runId}__`;
