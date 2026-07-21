import { defineConfig, devices } from '@playwright/test';
import { CONFIG } from './helpers/config';

// One browser harness, invoked in phases by scripts/compat.sh:
// baseline capture -> current verification -> current-only behavior.
export default defineConfig({
  testDir: './tests',
  globalSetup: require.resolve('./compat.setup'),
  globalTeardown: require.resolve('./compat.teardown'),
  timeout: 240_000,
  expect: { timeout: 45_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  outputDir: 'test-results',
  use: {
    baseURL: CONFIG.baseURL,
    headless: true,
    screenshot: 'on',
    trace: 'on',
    video: 'retain-on-failure',
    ignoreHTTPSErrors: true,
    viewport: { width: 1600, height: 1000 },
  },
  projects: [
    {
      name: 'capture',
      testMatch: /.*capture\.spec\.ts/,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'verify',
      testMatch: /.*verify\.spec\.ts/,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'current',
      testMatch: /tests\/(?!compat\/).*\.spec\.ts$/,
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
