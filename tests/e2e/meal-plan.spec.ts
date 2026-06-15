/**
 * Meal planning: the page loads authed, and a __E2E__ plan can be created via API,
 * seen in the plan list + UI, then deleted — verifying it disappears from both.
 */
import { test, expect, E2E_PREFIX } from './fixtures';

test('/meal-plan page loads authed', async ({ authedPage }) => {
  const resp = await authedPage.goto('/meal-plan');
  expect(resp?.ok(), `status ${resp?.status()}`).toBeTruthy();
});

test('create → list → delete a __E2E__ meal plan, verify in UI', async ({ authedPage, testUser }) => {
  const suffix = Math.random().toString(36).slice(2, 8);
  const name = `${E2E_PREFIX(testUser.runId)}plan-${suffix}`;

  const created = await authedPage.request.post('/api/meal-plan', {
    data: { name, start_date: '2026-01-05', plan_type: 'weekly' },
  });
  expect(created.ok(), `create returned ${created.status()}`).toBeTruthy();
  const body = await created.json();
  const planId = body.plan_id || body.id || (body.plan && (body.plan.id || body.plan.plan_id));
  expect(planId, `no plan id in response: ${JSON.stringify(body)}`).toBeTruthy();

  const listed = await authedPage.request.get('/api/meal-plan/list');
  expect(listed.ok()).toBeTruthy();
  const listBody = await listed.json();
  const plans = Array.isArray(listBody) ? listBody : listBody.plans || listBody.data || [];
  expect(plans.some((p) => p.name === name)).toBeTruthy();

  // Loading with ?plan_id makes our plan the active plan, so its name renders in
  // the visible header (the bare /meal-plan only lists names inside a <select>).
  await authedPage.goto(`/meal-plan?plan_id=${planId}`);
  await expect(authedPage.locator('body')).toContainText(name);

  const deleted = await authedPage.request.delete(`/api/meal-plan/${planId}`);
  expect(deleted.ok(), `delete returned ${deleted.status()}`).toBeTruthy();

  const after = await authedPage.request.get('/api/meal-plan/list');
  const afterBody = await after.json();
  const afterPlans = Array.isArray(afterBody) ? afterBody : afterBody.plans || afterBody.data || [];
  expect(afterPlans.some((p) => p.name === name)).toBeFalsy();
});
