/**
 * Workflow: every top-nav link reachable from dashboard goes somewhere that loads.
 * Catches the regression class of "renamed a route but didn't update the nav".
 */
import { test, expect } from '../fixtures';

test('top navigation links from /dashboard all resolve to 2xx', async ({ authedPage }) => {
  await authedPage.goto('/dashboard');
  const navLinks = await authedPage.locator('nav a, header a, [role=navigation] a').all();

  const seen = new Set<string>();
  const broken: { href: string; status: number }[] = [];

  for (const link of navLinks) {
    const href = await link.getAttribute('href');
    if (!href || seen.has(href)) continue;
    if (href.startsWith('http') || href.startsWith('#') || href.startsWith('mailto:')) continue;
    if (href === '/logout') continue;
    seen.add(href);
    const resp = await authedPage.request.get(href);
    if (!resp.ok() && resp.status() !== 302 && resp.status() !== 307) {
      broken.push({ href, status: resp.status() });
    }
  }

  expect(broken, `broken nav links: ${JSON.stringify(broken)}`).toEqual([]);
});
