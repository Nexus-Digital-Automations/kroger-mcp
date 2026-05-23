/**
 * Playwright config for the frontend-bug-finder suite.
 * Scoped to tests/e2e/ so it does not interfere with tests/playwright/ legacy scripts.
 * Run: npx playwright test --config=tests/e2e/playwright.config.ts
 */
import { defineConfig } from '@playwright/test';
import * as path from 'path';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8000';

export default defineConfig({
  testDir: __dirname,
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: false,
  workers: 1,
  reporter: [['list'], ['json', { outputFile: path.join(__dirname, '../../test-results/e2e-results.json') }]],
  globalSetup: require.resolve('./global-setup.ts'),
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
  expect: { timeout: 5_000 },
  timeout: 30_000,
  outputDir: path.join(__dirname, '../../test-results/e2e-artifacts'),
});
