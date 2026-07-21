import { test, expect, APIRequestContext, Page } from '@playwright/test';
import { plantCookie, listSessionItems } from '../../helpers/girder';
import {
  readCompatState,
  CompatState,
  CapturedGesture,
  ZipSummary,
} from '../../helpers/compat-state';
import { waitForVolViewReady, remoteSave, urlsParam, shot } from '../../helpers/volview';
import {
  isSessionManifest,
  resourceNames,
  reloadCapturingManifest,
} from '../../helpers/manifest';
import {
  gotoFolder,
  checkRowByItemId,
  checkRowByTexts,
  fillFilterBox,
  openInVolView,
  openFromItemPage,
  loginViaUI,
  VolViewLaunch,
} from '../../helpers/girder-ui';
import {
  readRulerMeasurements,
  readSegmentGroupNames,
  isLayered,
} from '../../helpers/annotations';
import { fetchZipSummary } from '../../helpers/session-zip';

// VERIFY phase — runs against THIS worktree's deploy, after the redeploy.
// Each captured main-era session must: resolve through the branch's manifest
// logic when the gesture is replayed, restore its content faithfully, and
// round-trip through a branch re-save + F5.

const PET_DESC = 'PET NAC OSEM';

function requireGesture(state: CompatState, id: CapturedGesture['id']): CapturedGesture {
  const gesture = state.gestures.find((g) => g.id === id);
  if (!gesture) throw new Error(`[compat] capture did not record gesture '${id}'`);
  return gesture;
}

const sortedLengths = (rulers: Array<{ lengthText: string }>) =>
  rulers.map((r) => r.lengthText).sort();

async function assertContentRestored(popup: Page, gesture: CapturedGesture): Promise<void> {
  const rulers = await readRulerMeasurements(popup);
  expect(
    sortedLengths(rulers),
    'restored ruler measurements must match the capture exactly (world coords live in the zip)'
  ).toEqual(sortedLengths(gesture.expected.rulers));

  if (gesture.expected.segmentGroupNames.length) {
    const groups = await readSegmentGroupNames(popup);
    for (const name of gesture.expected.segmentGroupNames) {
      expect(groups, `segment group "${name}" lost in restore`).toContain(name);
    }
  }

  if (gesture.expected.petLayer) {
    expect(await isLayered(popup, PET_DESC), 'PET layer lost in restore').toBeTruthy();
  }
}

// The main-era zip's content must round-trip through the BRANCH serializer:
// re-save and compare semantic summaries (schema migrations are fine; content
// loss is not).
function expectZipRoundTrip(fresh: ZipSummary, gesture: CapturedGesture): void {
  const captured = gesture.expected.zip;
  expect(fresh.rulerCount, 're-saved zip lost rulers').toBe(captured.rulerCount);
  expect(fresh.segmentGroupCount, 're-saved zip lost segment groups').toBeGreaterThanOrEqual(
    captured.segmentGroupCount
  );
  if (captured.segmentGroupDataBytes > 0) {
    expect(fresh.segmentGroupDataBytes, 're-saved labelmap is empty').toBeGreaterThan(0);
  }
  if (gesture.expected.petLayer) {
    expect(fresh.hasLayers, 're-saved zip lost the layer').toBeTruthy();
  }
}

async function expectResumedSession(launch: VolViewLaunch, sessionItemName?: string): Promise<void> {
  const m = await launch.manifest;
  expect(m, 'no manifest captured on launch').toBeTruthy();
  expect(
    isSessionManifest(m),
    `branch must resume the main-era session: ${resourceNames(m)}`
  ).toBeTruthy();
  if (sessionItemName) {
    expect(resourceNames(m), 'manifest names a different session').toContain(sessionItemName);
  }
}

// Branch re-save from a resumed main session, then F5 must reload the NEW save.
async function resaveAndReload(
  request: APIRequestContext,
  state: CompatState,
  gesture: CapturedGesture,
  popup: Page
): Promise<void> {
  const folderScoped = gesture.launch.via !== 'item-page';
  const before = new Set(
    (await listSessionItems(request, state.token, gesture.folderId)).map((i) => i._id)
  );

  const resumeUrl = await remoteSave(popup);
  expect(resumeUrl, 'branch save must return a resumeUrl').toBeTruthy();
  expect(urlsParam(popup)).toBe(resumeUrl);

  let zipItemId: string;
  if (folderScoped) {
    const after = await listSessionItems(request, state.token, gesture.folderId);
    const minted = after.filter((i) => !before.has(i._id));
    expect(minted.length, 'branch folder save should mint a new session item').toBe(1);
    zipItemId = minted[0]._id;
  } else {
    zipItemId = (gesture.launch as { itemId: string }).itemId;
  }
  expectZipRoundTrip(await fetchZipSummary(request, state.token, zipItemId), gesture);

  const m = await reloadCapturingManifest(popup);
  expect(urlsParam(popup), 'F5 after the branch save must stay on its resumeUrl').toBe(resumeUrl);
  if (m) {
    expect(isSessionManifest(m), 'F5 must resume the branch save').toBeTruthy();
  }
  await assertContentRestored(popup, gesture);
}

test.describe('compat verify (against branch deploy)', () => {
  let state: CompatState;

  test.beforeEach(async ({ context, page }) => {
    const s = readCompatState();
    if (!s) throw new Error('[compat] no state — run the capture phase first');
    state = s;
    await plantCookie(context, state.token);
    await loginViaUI(page);
  });

  test('single-item: item manifest serves the main-era session', async ({ page, request }, info) => {
    const gesture = requireGesture(state, 'single-item');
    const { itemId } = gesture.launch as { itemId: string };

    const launch = await openFromItemPage(page, itemId);
    await expectResumedSession(launch);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'verify-single-item-restored');
    await assertContentRestored(launch.popup, gesture);

    await resaveAndReload(request, state, gesture, launch.popup);
  });

  test('checked-nrrd: bare-folder open resumes, session-row open targets it', async ({
    page,
    request,
  }, info) => {
    const gesture = requireGesture(state, 'checked-nrrd');

    // Bare folder-open (nothing checked) must resume the newest session — the
    // one main saved.
    await gotoFolder(page, gesture.folderId);
    const launch = await openInVolView(page);
    await expectResumedSession(launch, gesture.sessionItemName);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'verify-checked-nrrd-restored');
    await assertContentRestored(launch.popup, gesture);

    // Checking the main-era session item (plus a raw image) in the girder UI
    // must open exactly that session.
    await gotoFolder(page, gesture.folderId);
    await checkRowByItemId(page, gesture.sessionItemId!);
    await checkRowByItemId(page, (gesture.launch as { itemIds: string[] }).itemIds[0]);
    const viaRow = await openInVolView(page);
    await waitForVolViewReady(viaRow.popup);
    expect(urlsParam(viaRow.popup)).toContain(`items=${gesture.sessionItemId}`);
    await viaRow.popup.close();

    await resaveAndReload(request, state, gesture, launch.popup);
  });

  test('filtered-dicom: replaying the filter gesture resumes the matching session', async ({
    page,
    request,
  }, info) => {
    const gesture = requireGesture(state, 'filtered-dicom');
    const launch0 = gesture.launch as { rows: string[][]; filterText?: string };

    await gotoFolder(page, gesture.folderId);
    if (launch0.filterText) await fillFilterBox(page, launch0.filterText);
    for (const row of launch0.rows) await checkRowByTexts(page, row);
    const launch = await openInVolView(page);
    await expectResumedSession(launch, gesture.sessionItemName);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'verify-filtered-dicom-restored');
    await assertContentRestored(launch.popup, gesture);

    await resaveAndReload(request, state, gesture, launch.popup);
  });

  test('study-layered: replaying the CT+PET selection resumes with the layer', async ({
    page,
    request,
  }, info) => {
    const gesture = requireGesture(state, 'study-layered');
    const launch0 = gesture.launch as { rows: string[][] };

    await gotoFolder(page, gesture.folderId);
    for (const row of launch0.rows) await checkRowByTexts(page, row);
    const launch = await openInVolView(page);
    await expectResumedSession(launch, gesture.sessionItemName);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'verify-study-layered-restored');
    await assertContentRestored(launch.popup, gesture);

    await resaveAndReload(request, state, gesture, launch.popup);
  });
});
