import { defineConfig, devices } from '@playwright/test';
import { CONFIG } from './helpers/config';

// One browser harness, invoked in phases by scripts/compat.sh:
// baseline capture -> current verification -> current-only behavior.
export default defineConfig({
  testDir: './tests',
  globalSetup: require.resolve('./compat.setup'),
  globalTeardown: require.resolve('./compat.teardown'),
  // The suite's time limits, set once: specs and helpers wait on page state
  // and inherit them. An assertion gets a minute, enough for a PET layer to
  // finish loading over its CT. Any other wait gets 90 s (actionTimeout),
  // enough for a VolView popup to boot and fetch its manifest. Job outcomes
  // use helpers/limits.ts.
  timeout: 240_000,
  expect: { timeout: 60_000 },
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
    actionTimeout: 90_000,
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
