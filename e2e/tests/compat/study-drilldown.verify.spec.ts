import { test, expect } from '@playwright/test';
import { plantCookie, listSessionItems } from '../../helpers/girder';
import { readCompatState, CompatState } from '../../helpers/compat-state';
import { waitForVolViewReady, remoteSave, urlsParam, shot } from '../../helpers/volview';
import { isSessionManifest, resourceNames } from '../../helpers/manifest';
import { gotoFolder, drillRowNav, loginViaUI } from '../../helpers/girder-ui';
import { readRulerMeasurements, isLayered } from '../../helpers/annotations';
import { fetchZipSummary } from '../../helpers/session-zip';
import { apiUrl } from '../../helpers/config';

const PET_DESC = 'PET NAC OSEM';

test.describe('compat verify: study drill-down', () => {
  let state: CompatState;

  test.beforeEach(async ({ context, page }) => {
    const s = readCompatState();
    if (!s) throw new Error('[compat] no state — run the capture phase first');
    state = s;
    await plantCookie(context, state.token);
    await loginViaUI(page);
  });

  test('study-drilldown: replaying the drill-down resumes the session with its layer', async ({
    page,
    request,
  }, info) => {
    const gesture = state.gestures.find((g) => g.id === 'study-drilldown');
    if (!gesture) throw new Error('[compat] capture did not record study drill-down');
    const { rowTexts } = gesture.launch as { rowTexts: string[] };
    const folderId = gesture.folderId;

    await gotoFolder(page, folderId);
    const launch = await drillRowNav(page, rowTexts);
    const m = await launch.manifest;
    expect(
      isSessionManifest(m),
      `study drill-down must resume the main-era session: ${resourceNames(m)}`
    ).toBeTruthy();
    expect(resourceNames(m)).toContain(gesture.sessionItemName);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'verify-study-drilldown-restored');

    const rulers = await readRulerMeasurements(launch.popup);
    expect(rulers.map((r) => r.lengthText).sort()).toEqual(
      gesture.expected.rulers.map((r) => r.lengthText).sort()
    );
    expect(await isLayered(launch.popup, PET_DESC), 'PET layer lost in restore').toBeTruthy();

    // Branch re-save round-trip.
    const before = new Set(
      (await listSessionItems(request, state.token, folderId)).map((i) => i._id)
    );
    const resumeUrl = await remoteSave(launch.popup);
    expect(resumeUrl, 'branch save must return a resumeUrl').toBeTruthy();
    expect(urlsParam(launch.popup)).toBe(resumeUrl);
    const minted = (await listSessionItems(request, state.token, folderId)).filter(
      (i) => !before.has(i._id)
    );
    expect(minted.length).toBe(1);
    const zip = await fetchZipSummary(request, state.token, minted[0]._id);
    expect(zip.rulerCount).toBe(gesture.expected.zip.rulerCount);
    expect(zip.hasLayers).toBeTruthy();

    // Remove the session items this run minted so a kept fixture can be
    // verified again without changing newest-session selection.
    for (const itemId of [gesture.sessionItemId, minted[0]._id]) {
      if (!itemId) continue;
      await request.delete(apiUrl(`/item/${itemId}`), {
        headers: { 'Girder-Token': state.token },
      });
    }
  });
});
