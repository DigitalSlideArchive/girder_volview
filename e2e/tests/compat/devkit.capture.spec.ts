import { test, expect } from '@playwright/test';
import { plantCookie, listSessionItems } from '../../helpers/girder';
import { readCompatState, appendGesture, CompatState } from '../../helpers/compat-state';
import { waitForVolViewReady, remoteSave, shot } from '../../helpers/volview';
import { isSessionManifest } from '../../helpers/manifest';
import { gotoFolder, drillRowNav, loginViaUI } from '../../helpers/girder-ui';
import {
  placeRuler,
  readRulerMeasurements,
  readDatasetNames,
  selectPrimaryVolume,
  addLayer,
} from '../../helpers/annotations';
import { fetchZipSummary } from '../../helpers/session-zip';
import { apiUrl } from '../../helpers/config';

// Tier 2 (optional): the fully-seeded devkit collection — real patient→study
// drill-down through the .large_image_config.yaml hierarchy, whole-study
// CT+PET launch. Skipped unless `seed.py seed` has run against this stack.

const PATIENT1 = 'ACRIN-NSCLC-FDG-PET-017';
const CT_DESC = 'CT IMAGES';
const PET_DESC = 'PET NAC OSEM';

test.describe('compat capture: devkit study drill-down', () => {
  let state: CompatState;

  test.beforeEach(async ({ context, page }) => {
    const s = readCompatState();
    if (!s) throw new Error('[compat] no state — did compat.setup run in capture phase?');
    state = s;
    await plantCookie(context, state.token);
    await loginViaUI(page);
  });

  test('devkit-study: patient → study row opens whole study; layer + ruler; save', async ({
    page,
    request,
  }, info) => {
    test.skip(!state.devkitTrialFolderId, 'VolView Devkit collection not seeded (optional tier)');
    const folderId = state.devkitTrialFolderId!;
    const rowTexts = [PATIENT1, PATIENT1]; // patient row, then its first study row

    // The devkit collection is shared and persistent — session zips left by
    // earlier runs make the drill-down resume instead of loading fresh. Clear
    // them so capture is idempotent.
    const stale = await listSessionItems(request, state.token, folderId);
    for (const item of stale) {
      await request.delete(apiUrl(`/item/${item._id}`), {
        headers: { 'Girder-Token': state.token },
      });
    }

    await gotoFolder(page, folderId);
    const launch = await drillRowNav(page, rowTexts);
    const m = await launch.manifest;
    if (m) expect(isSessionManifest(m), 'devkit study launch must be fresh').toBeFalsy();
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'capture-devkit-study-loaded');

    await selectPrimaryVolume(launch.popup, CT_DESC);
    await addLayer(launch.popup, PET_DESC);
    await placeRuler(launch.popup);
    const rulers = await readRulerMeasurements(launch.popup);
    expect(rulers.length).toBe(1);
    const datasetNames = await readDatasetNames(launch.popup);
    await shot(launch.popup, info, 'capture-devkit-study-content');

    const before = new Set(
      (await listSessionItems(request, state.token, folderId)).map((i) => i._id)
    );
    await remoteSave(launch.popup);
    const minted = (await listSessionItems(request, state.token, folderId)).filter(
      (i) => !before.has(i._id)
    );
    expect(minted.length, 'devkit study save should mint one session item').toBe(1);

    const zip = await fetchZipSummary(request, state.token, minted[0]._id);
    expect(zip.rulerCount).toBe(1);
    expect(zip.hasLayers).toBeTruthy();

    appendGesture({
      id: 'devkit-study',
      folderId,
      launch: { via: 'row-nav', rowTexts },
      sessionItemId: minted[0]._id,
      sessionItemName: minted[0].name,
      expected: { datasetNames, rulers, segmentGroupNames: [], petLayer: true, zip },
    });
  });
});
