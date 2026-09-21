import { expect } from '@playwright/test';

// A processing job runs a CLI container end to end, far longer than the
// assertion limit playwright.config.ts sets for everything else. Waits on a
// job's outcome use this instead; no other file sets a time limit.
export const expectJob = expect.configure({ timeout: 180_000 });
