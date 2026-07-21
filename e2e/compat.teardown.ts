import { request as playwrightRequest, FullConfig } from '@playwright/test';
import { readCompatState, clearCompatState } from './helpers/compat-state';
import { teardownCompat } from './helpers/compat-provision';

// The orchestrator chooses the final Playwright invocation by setting
// COMPAT_CLEANUP=1. Earlier phases leave the run root and state in place.
export default async function compatTeardown(_config: FullConfig): Promise<void> {
  if (process.env.COMPAT_CLEANUP !== '1') return;
  if (process.env.COMPAT_KEEP === '1') {
    // eslint-disable-next-line no-console
    console.log('[compat] COMPAT_KEEP=1 — keeping run folder and state for iteration.');
    return;
  }

  const state = readCompatState();
  if (!state) return;
  if (state.provisioned) {
    const request = await playwrightRequest.newContext({ ignoreHTTPSErrors: true });
    try {
      await teardownCompat(request, state);
    } finally {
      await request.dispose();
    }
  }
  clearCompatState();
}
