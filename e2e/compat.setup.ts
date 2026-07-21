import { request as playwrightRequest, FullConfig } from '@playwright/test';
import { healthCheck, verifyDeployedHeads, fetchDeployReceipt } from './helpers/stack';
import { provisionCompat } from './helpers/compat-provision';
import { readCompatState, writeCompatState, COMPAT_STATE_PATH } from './helpers/compat-state';

// Phase-switched global setup for the browser harness.
//
//   COMPAT_PHASE=capture  — against the MAIN deploy (E2E_EXPECT_GIRDER_SHA and
//                           E2E_EXPECT_VOLVIEW_SHA carry the pinned pair):
//                           provision the run folder ONCE and write
//                           .compat-state.json.
//   COMPAT_PHASE=verify   — against THIS worktree: restore captured sessions.
//   COMPAT_PHASE=current  — against THIS worktree: exercise fresh current
//                           lifecycles and jobs in otherwise untouched folders.
export default async function compatSetup(_config: FullConfig): Promise<void> {
  const phase = process.env.COMPAT_PHASE;
  if (phase !== 'capture' && phase !== 'verify' && phase !== 'current') {
    throw new Error(
      `[compat] COMPAT_PHASE must be 'capture', 'verify', or 'current' (got '${phase ?? ''}'). ` +
        'Run via e2e/scripts/compat.sh.'
    );
  }

  const request = await playwrightRequest.newContext({ ignoreHTTPSErrors: true });
  try {
    await healthCheck(request);
    await verifyDeployedHeads(request);

    if (phase === 'capture') {
      if (readCompatState()) {
        throw new Error(
          `[compat] ${COMPAT_STATE_PATH} already exists — a previous capture was not ` +
            'verified/torn down. Run the verify phase (or delete the state file and its ' +
            'run folder) first.'
        );
      }
      const receipt = await fetchDeployReceipt(request);
      const state = await provisionCompat(request, {
        sourceGirderSha: receipt.girderSha || '',
        sourceVolviewSha: receipt.volviewSha || '',
      });
      writeCompatState(state);
      // eslint-disable-next-line no-console
      console.log(
        `[compat] capture provisioned: root ${state.runRootFolderId} ` +
          `with ${Object.keys(state.fixtures).length} isolated fixture folders ` +
          `against source girder ${state.sourceGirderSha.slice(0, 9)}`
      );
    } else {
      const state = readCompatState();
      if (!state?.provisioned) {
        throw new Error('[compat] no .compat-state.json — run the capture phase first.');
      }
      if (phase === 'verify' && state.gestures.length === 0) {
        throw new Error('[compat] capture recorded no gestures — nothing to verify.');
      }
      // eslint-disable-next-line no-console
      console.log(
        `[compat] ${phase === 'verify' ? 'verifying' : 'running current scenarios with'} ` +
          `${state.gestures.length} captured gesture(s) from source girder ` +
          `${state.sourceGirderSha.slice(0, 9)}`
      );
    }
  } finally {
    await request.dispose();
  }
}
