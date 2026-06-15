/**
 * AI chat — contract-only, offline-safe (never calls the LLM):
 *  - an empty message is rejected with 400 before any provider is contacted,
 *  - the providers endpoint returns a well-formed shape (empty list is valid when
 *    no provider API key is configured),
 *  - the chat widget (mounted in base.html) renders on an authed page.
 */
import { test, expect } from './fixtures';

test('POST /api/chat/message with an empty message returns 400', async ({ authedPage }) => {
  const resp = await authedPage.request.post('/api/chat/message', {
    data: { user_message: '   ', messages: [] },
  });
  expect(resp.status()).toBe(400);
});

test('GET /api/chat/providers returns { providers: [], default } shape', async ({ authedPage }) => {
  const resp = await authedPage.request.get('/api/chat/providers');
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  expect(Array.isArray(body.providers)).toBeTruthy();
  expect(typeof body.default).toBe('string');
});

test('chat widget renders on /dashboard', async ({ authedPage }) => {
  await authedPage.goto('/dashboard');
  await expect(authedPage.locator('.chat-fab')).toBeVisible();
});
