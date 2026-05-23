/**
 * Production guardrail: refuse to run against any host that isn't a known dev/staging origin.
 * Also asserts the dev server is reachable before the suite starts.
 */
import { FullConfig, request } from '@playwright/test';

const ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'staging', 'dev', 'preview'];

export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0]?.use?.baseURL || process.env.E2E_BASE_URL;
  if (!baseURL) throw new Error('E2E_BASE_URL is required.');

  const host = new URL(baseURL).hostname;
  const ok = ALLOWED_HOSTS.some((h) => host.includes(h));
  if (!ok && process.env.ALLOW_PRODUCTION !== '1') {
    throw new Error(
      `Refusing to run against non-dev host: ${host}. ` +
        `Allowed hosts must contain one of: ${ALLOWED_HOSTS.join(', ')}.`,
    );
  }

  const ctx = await request.newContext({ baseURL });
  try {
    const res = await ctx.get('/login');
    if (!res.ok()) throw new Error(`Dev server at ${baseURL} returned ${res.status()} on /login`);
  } finally {
    await ctx.dispose();
  }
}
