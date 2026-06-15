/**
 * Best-effort teardown: deletes any __E2E__{runId}__ entities that survived test runs,
 * then removes the throwaway account.json so the next run starts clean.
 *
 * The app has no delete-account endpoint, so the SQLite/Postgres user row will remain.
 * That's safe: the email is randomized per run, never collides, and is gitignored.
 */
import { chromium } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8000';
const ACCOUNT_FILE = path.join(__dirname, '..', '_discovery', 'account.json');

interface Account {
  runId: string;
  email: string;
  password: string;
  displayName: string;
}

async function cleanup(): Promise<void> {
  if (!fs.existsSync(ACCOUNT_FILE)) {
    console.log('[teardown] no account.json — nothing to clean');
    return;
  }
  const account = JSON.parse(fs.readFileSync(ACCOUNT_FILE, 'utf8')) as Account;
  const prefix = `__E2E__${account.runId}__`;

  const browser = await chromium.launch();
  const ctx = await browser.newContext();

  try {
    const loginPage = await ctx.newPage();
    await loginPage.goto(`${BASE_URL}/login`);
    await loginPage.locator('input[name=email]').fill(account.email);
    await loginPage.locator('input[name=password]').fill(account.password);
    await Promise.all([
      loginPage.waitForURL((u) => !/\/login$/.test(u.pathname), { timeout: 5_000 }).catch(() => undefined),
      loginPage.locator('button[type=submit]').click(),
    ]);

    const apiReq = await ctx.request;
    const favs = await apiReq.get(`${BASE_URL}/api/favorites/lists`);
    if (favs.ok()) {
      const body = await favs.json();
      const lists = Array.isArray(body) ? body : body.lists || body.data || [];
      for (const list of lists) {
        const name = list.name || list.list_name;
        if (typeof name === 'string' && name.startsWith(prefix)) {
          await apiReq.delete(`${BASE_URL}/api/favorites/lists/${list.id || list.list_id}`);
          console.log(`[teardown] deleted favorites list "${name}"`);
        }
      }
    }

    const ings = await apiReq.get(`${BASE_URL}/api/ingredients/custom`);
    if (ings.ok()) {
      const body = await ings.json();
      const items = Array.isArray(body) ? body : body.ingredients || body.data || [];
      for (const item of items) {
        if (typeof item.name === 'string' && item.name.startsWith(prefix)) {
          await apiReq.delete(`${BASE_URL}/api/ingredients/custom/${encodeURIComponent(item.name)}`);
          console.log(`[teardown] deleted ingredient "${item.name}"`);
        }
      }
    }

    const shop = await apiReq.get(`${BASE_URL}/api/shopping-list`);
    if (shop.ok()) {
      const body = await shop.json();
      const items = Array.isArray(body) ? body : body.items || body.data || [];
      for (const item of items) {
        if (typeof item.name === 'string' && item.name.startsWith(prefix)) {
          await apiReq.delete(`${BASE_URL}/api/shopping-list/${item.id || item.item_id}`);
          console.log(`[teardown] deleted shopping-list item "${item.name}"`);
        }
      }
    }

    // Meal plans are matched by name; deals watchlist + safe-list by description.
    // (Pantry has no JSON list endpoint, so its spec self-deletes by product_id.)
    const plans = await apiReq.get(`${BASE_URL}/api/meal-plan/list`);
    if (plans.ok()) {
      const body = await plans.json();
      const items = Array.isArray(body) ? body : body.plans || body.data || [];
      for (const plan of items) {
        if (typeof plan.name === 'string' && plan.name.startsWith(prefix)) {
          await apiReq.delete(`${BASE_URL}/api/meal-plan/${plan.id || plan.plan_id}`);
          console.log(`[teardown] deleted meal plan "${plan.name}"`);
        }
      }
    }

    const watch = await apiReq.get(`${BASE_URL}/api/deals/watchlist`);
    if (watch.ok()) {
      const body = await watch.json();
      const items = Array.isArray(body) ? body : body.data || [];
      for (const row of items) {
        if (typeof row.description === 'string' && row.description.startsWith(prefix)) {
          await apiReq.delete(`${BASE_URL}/api/deals/watchlist/${encodeURIComponent(row.product_id)}`);
          console.log(`[teardown] deleted watchlist item "${row.description}"`);
        }
      }
    }

    const approved = await apiReq.get(`${BASE_URL}/api/safety/approved`);
    if (approved.ok()) {
      const body = await approved.json();
      const items = Array.isArray(body) ? body : body.data || [];
      for (const row of items) {
        if (typeof row.description === 'string' && row.description.startsWith(prefix)) {
          await apiReq.delete(`${BASE_URL}/api/safety/approved/${encodeURIComponent(row.product_id)}`);
          console.log(`[teardown] deleted safe-listed product "${row.description}"`);
        }
      }
    }
  } finally {
    await ctx.close();
    await browser.close();
  }

  fs.unlinkSync(ACCOUNT_FILE);
  console.log('[teardown] account.json removed');
}

cleanup().catch((err) => {
  console.error('[teardown] failed:', err);
  process.exit(1);
});
