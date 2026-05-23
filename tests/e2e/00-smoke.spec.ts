/**
 * Smoke pass: every HTML page must load with HTTP 200, no console errors, no 5xx network responses.
 * Catches the most common failure modes (missing template var, broken template, 500 in handler).
 */
import { test, expect } from './fixtures';

const HTML_ROUTES = [
  '/',
  '/dashboard',
  '/recipes',
  '/meal-plan',
  '/favorites',
  '/pantry',
  '/products',
  '/shopping-list',
  '/deals',
  '/safety',
  '/ingredients',
  '/settings',
  '/login',
  '/register',
];

for (const route of HTML_ROUTES) {
  test(`smoke: ${route} loads without console or server errors`, async ({ authedPage }) => {
    const consoleErrors: string[] = [];
    const networkErrors: { url: string; status: number }[] = [];

    authedPage.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    authedPage.on('response', (resp) => {
      const status = resp.status();
      if (status >= 500) networkErrors.push({ url: resp.url(), status });
    });

    const response = await authedPage.goto(route);
    expect(response, `no response for ${route}`).not.toBeNull();
    expect(response!.status(), `${route} returned ${response!.status()}`).toBeLessThan(400);

    await authedPage.waitForLoadState('networkidle', { timeout: 8_000 }).catch(() => {});

    expect(consoleErrors, `console errors on ${route}: ${consoleErrors.join(' | ')}`).toEqual([]);
    expect(networkErrors, `5xx responses on ${route}: ${JSON.stringify(networkErrors)}`).toEqual([]);
  });
}
