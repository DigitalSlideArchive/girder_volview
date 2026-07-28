import { test, expect } from '@playwright/test';
import { plantCookie, listSessionItems } from '../../helpers/girder';
import {
  readCompatState,
  appendGesture,
  CompatState,
  requireFixture,
} from '../../helpers/compat-state';
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

// Patient → study drill-down through an isolated, automatically provisioned
// small-tier hierarchy, followed by a whole-study CT+PET launch.

const PATIENT1 = 'ACRIN-NSCLC-FDG-PET-017';
const STUDY1 = 'PET/CT';
const CT_DESC = 'CT IMAGES';
const PET_DESC = 'PET NAC OSEM';

test.describe('compat capture: study drill-down', () => {
  let state: CompatState;

  test.beforeEach(async ({ context, page }) => {
    const s = readCompatState();
    if (!s) throw new Error('[compat] no state — did compat.setup run in capture phase?');
    state = s;
    await plantCookie(context, state.token);
    await loginViaUI(page);
  });

  test('study-drilldown: patient → study row opens whole study; layer + ruler; save', async ({
    page,
    request,
  }, info) => {
    const fixture = requireFixture(state, 'study-drilldown');
    const folderId = fixture.folderId;
    const rowTexts = [PATIENT1, STUDY1]; // patient row, then one pinned study row

    await gotoFolder(page, folderId);
    const launch = await drillRowNav(page, rowTexts);
    const m = await launch.manifest;
    expect(isSessionManifest(m), 'study launch must be fresh').toBeFalsy();
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'capture-study-drilldown-loaded');

    await selectPrimaryVolume(launch.popup, CT_DESC);
    await addLayer(launch.popup, PET_DESC);
    await placeRuler(launch.popup);
    const rulers = await readRulerMeasurements(launch.popup);
    expect(rulers.length).toBe(1);
    const datasetNames = await readDatasetNames(launch.popup);
    await shot(launch.popup, info, 'capture-study-drilldown-content');

    const before = new Set(
      (await listSessionItems(request, state.token, folderId)).map((i) => i._id)
    );
    await remoteSave(launch.popup);
    const minted = (await listSessionItems(request, state.token, folderId)).filter(
      (i) => !before.has(i._id)
    );
    expect(minted.length, 'study save should mint one session item').toBe(1);

    const zip = await fetchZipSummary(request, state.token, minted[0]._id);
    expect(zip.rulerCount).toBe(1);
    expect(zip.hasLayers).toBeTruthy();

    appendGesture({
      id: 'study-drilldown',
      folderId,
      launch: { via: 'row-nav', rowTexts },
      sessionItemId: minted[0]._id,
      sessionItemName: minted[0].name,
      expected: { datasetNames, rulers, segmentGroupNames: [], petLayer: true, zip },
    });
  });
});
