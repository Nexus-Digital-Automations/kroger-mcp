/**
 * Provision a throwaway local user account by driving /register through Playwright.
 *
 * Writes credentials to tests/e2e/_discovery/account.json (gitignored).
 * Exits non-zero if registration fails so the suite cannot proceed with stale creds.
 */
import { chromium } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8000';
const OUT_DIR = path.join(__dirname, '..', '_discovery');
const OUT_FILE = path.join(OUT_DIR, 'account.json');

function makeRunId(): string {
  const ts = Date.now().toString(36);
  const rand = Math.random().toString(36).slice(2, 6);
  return `${ts}${rand}`;
}

async function provision(): Promise<void> {
  if (fs.existsSync(OUT_FILE)) {
    console.log(`[provision] account.json already exists — skipping.`);
    return;
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });

  const runId = makeRunId();
  const email = `e2e-${runId}@example.test`;
  const password = `Pw!E2E${runId}`;
  const displayName = `__E2E__${runId}__user`;

  const browser = await chromium.launch();
  const ctx = await browser.newContext();
  const page = await ctx.newPage();

  try {
    await page.goto(`${BASE_URL}/register`);
    await page.locator('input[name="display_name"]').fill(displayName);
    await page.locator('input[name="email"]').fill(email);
    await page.locator('input[name="password"]').fill(password);
    await page.locator('input[name="confirm_password"]').fill(password);

    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().includes('/register') && r.request().method() === 'POST'),
      page.locator('button[type="submit"]').click(),
    ]);

    if (response.status() >= 400) {
      const body = await response.text();
      throw new Error(`Register failed: HTTP ${response.status()} — ${body.slice(0, 200)}`);
    }

    await page.waitForURL((url) => !/\/register$/.test(url.pathname), { timeout: 5_000 });

    const account = { runId, email, password, displayName };
    fs.writeFileSync(OUT_FILE, JSON.stringify(account, null, 2));
    console.log(`[provision] Created ${email} (runId=${runId})`);
  } finally {
    await ctx.close();
    await browser.close();
  }
}

provision().catch((err) => {
  console.error(err);
  process.exit(1);
});
