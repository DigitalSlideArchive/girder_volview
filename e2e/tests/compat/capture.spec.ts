import { test, expect, APIRequestContext, Page } from '@playwright/test';
import { plantCookie, listSessionItems } from '../../helpers/girder';
import {
  readCompatState,
  appendGesture,
  CompatState,
  GestureId,
  LaunchDescriptor,
  RulerRecord,
  requireFixture,
} from '../../helpers/compat-state';
import { waitForVolViewReady, remoteSave, shot } from '../../helpers/volview';
import { isSessionManifest, resourceNames } from '../../helpers/manifest';
import {
  gotoFolder,
  checkRowByItemId,
  checkRowByTexts,
  fillFilterBox,
  expectRow,
  expectNoRow,
  openInVolView,
  openFromItemPage,
  loginViaUI,
  VolViewLaunch,
} from '../../helpers/girder-ui';
import {
  placeRuler,
  readRulerMeasurements,
  paintStrokes,
  readSegmentGroupNames,
  readDatasetNames,
  selectPrimaryVolume,
  addLayer,
} from '../../helpers/annotations';
import { fetchZipSummary } from '../../helpers/session-zip';

// CAPTURE phase — runs against the MAIN deploy. Each test drives a real girder
// UI gesture, creates content in main's client, saves, and records the session
// item + expected content into .compat-state.json for the verify phase.
//
// Only main-era affordances may be used here: main's folder save returns NO
// resumeUrl, so session items are discovered by folder-listing diff.

const PATIENT1 = 'ACRIN-NSCLC-FDG-PET-017';
const PATIENT2 = 'ACRIN-NSCLC-FDG-PET-022';
const CT_DESC = 'CT IMAGES';
const PET_DESC = 'PET NAC OSEM';

function requireState(): CompatState {
  const state = readCompatState();
  if (!state) throw new Error('[compat] no state — did compat.setup run in capture phase?');
  return state;
}

async function expectFresh(launch: VolViewLaunch): Promise<void> {
  const m = await launch.manifest;
  expect(
    isSessionManifest(m),
    `capture launch must load raw images, not a session: ${resourceNames(m)}`
  ).toBeFalsy();
}

// Save, then identify the session item the folder-scoped save minted.
async function saveAndDiffSession(
  request: APIRequestContext,
  token: string,
  folderId: string,
  popup: Page
): Promise<{ sessionItemId: string; sessionItemName: string }> {
  const before = new Set((await listSessionItems(request, token, folderId)).map((i) => i._id));
  await remoteSave(popup);
  const after = await listSessionItems(request, token, folderId);
  const minted = after.filter((i) => !before.has(i._id));
  expect(minted.length, 'folder save should mint exactly one new session item').toBe(1);
  return { sessionItemId: minted[0]._id, sessionItemName: minted[0].name };
}

type CapturedContent = {
  datasetNames: string[];
  rulers: RulerRecord[];
  segmentGroupNames: string[];
  petLayer: boolean;
};

function record(
  id: GestureId,
  folderId: string,
  launch: LaunchDescriptor,
  content: CapturedContent,
  zip: Awaited<ReturnType<typeof fetchZipSummary>>,
  session?: { sessionItemId: string; sessionItemName: string }
): void {
  appendGesture({
    id,
    folderId,
    launch,
    sessionItemId: session?.sessionItemId,
    sessionItemName: session?.sessionItemName,
    expected: { ...content, zip },
  });
}

test.describe('compat capture (against main deploy)', () => {
  let state: CompatState;

  test.beforeEach(async ({ context, page }) => {
    state = requireState();
    await plantCookie(context, state.token);
    await loginViaUI(page);
  });

  test('single-item: ruler, item-scoped save', async ({ page, request }, info) => {
    const fixture = requireFixture(state, 'single-item');
    const itemId = fixture.itemIds[0];
    const launch = await openFromItemPage(page, itemId);
    await expectFresh(launch);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'capture-single-item-loaded');

    await placeRuler(launch.popup);
    const rulers = await readRulerMeasurements(launch.popup);
    expect(rulers.length).toBe(1);
    const datasetNames = await readDatasetNames(launch.popup);
    await shot(launch.popup, info, 'capture-single-item-content');

    await remoteSave(launch.popup);
    const zip = await fetchZipSummary(request, state.token, itemId);
    expect(zip.rulerCount).toBe(1);

    record(
      'single-item',
      fixture.folderId,
      { via: 'item-page', itemId },
      { datasetNames, rulers, segmentGroupNames: [], petLayer: false },
      zip
    );
  });

  test('checked-nrrd: ruler + painted segment group, folder save', async ({ page, request }, info) => {
    const fixture = requireFixture(state, 'checked-nrrd');
    const itemIds = fixture.itemIds;
    await gotoFolder(page, fixture.folderId);
    for (const id of itemIds) await checkRowByItemId(page, id);
    const launch = await openInVolView(page);
    await expectFresh(launch);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'capture-checked-nrrd-loaded');

    await placeRuler(launch.popup);
    const rulers = await readRulerMeasurements(launch.popup);
    expect(rulers.length).toBe(1);

    await paintStrokes(launch.popup);
    const segmentGroupNames = await readSegmentGroupNames(launch.popup);
    expect(segmentGroupNames.length).toBeGreaterThan(0);
    const datasetNames = await readDatasetNames(launch.popup);
    await shot(launch.popup, info, 'capture-checked-nrrd-content');

    const session = await saveAndDiffSession(request, state.token, fixture.folderId, launch.popup);
    const zip = await fetchZipSummary(request, state.token, session.sessionItemId);
    expect(zip.rulerCount).toBe(1);
    expect(zip.segmentGroupCount).toBeGreaterThan(0);
    expect(zip.segmentGroupDataBytes, 'painted labelmap should be non-trivial').toBeGreaterThan(0);

    record(
      'checked-nrrd',
      fixture.folderId,
      { via: 'checked-items', itemIds },
      { datasetNames, rulers, segmentGroupNames, petLayer: false },
      zip,
      session
    );
  });

  test('filtered-dicom: filter box narrows, ruler, filter-linked save', async ({ page, request }, info) => {
    const fixture = requireFixture(state, 'filtered-dicom');
    await gotoFolder(page, fixture.folderId);
    // Three series rows: p1 CT, p1 PET, p2 CT.
    await expectRow(page, [PATIENT1, CT_DESC]);
    await expectRow(page, [PATIENT1, PET_DESC]);
    await expectRow(page, [PATIENT2, CT_DESC]);
    await fillFilterBox(page, PATIENT2);
    await expectNoRow(page, [PATIENT1]);
    await expectRow(page, [PATIENT2, CT_DESC]);
    await checkRowByTexts(page, [PATIENT2]);
    const launch = await openInVolView(page);
    await expectFresh(launch);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'capture-filtered-dicom-loaded');

    await placeRuler(launch.popup);
    const rulers = await readRulerMeasurements(launch.popup);
    expect(rulers.length).toBe(1);
    const datasetNames = await readDatasetNames(launch.popup);
    await shot(launch.popup, info, 'capture-filtered-dicom-content');

    const session = await saveAndDiffSession(request, state.token, fixture.folderId, launch.popup);
    const zip = await fetchZipSummary(request, state.token, session.sessionItemId);
    expect(zip.rulerCount).toBe(1);

    record(
      'filtered-dicom',
      fixture.folderId,
      { via: 'checked-rows', rows: [[PATIENT2]], filterText: PATIENT2 },
      { datasetNames, rulers, segmentGroupNames: [], petLayer: false },
      zip,
      session
    );
  });

  test('study-layered: CT+PET checked, PET layered over CT, ruler', async ({ page, request }, info) => {
    const fixture = requireFixture(state, 'study-layered');
    const rows = [
      [PATIENT1, CT_DESC],
      [PATIENT1, PET_DESC],
    ];
    await gotoFolder(page, fixture.folderId);
    for (const row of rows) await checkRowByTexts(page, row);
    const launch = await openInVolView(page);
    await expectFresh(launch);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'capture-study-layered-loaded');

    await selectPrimaryVolume(launch.popup, CT_DESC);
    await addLayer(launch.popup, PET_DESC);
    await placeRuler(launch.popup);
    const rulers = await readRulerMeasurements(launch.popup);
    expect(rulers.length).toBe(1);
    const datasetNames = await readDatasetNames(launch.popup);
    await shot(launch.popup, info, 'capture-study-layered-content');

    const session = await saveAndDiffSession(request, state.token, fixture.folderId, launch.popup);
    const zip = await fetchZipSummary(request, state.token, session.sessionItemId);
    expect(zip.rulerCount).toBe(1);
    expect(zip.hasLayers, 'the PET layer should serialize into the session').toBeTruthy();

    record(
      'study-layered',
      fixture.folderId,
      { via: 'checked-rows', rows },
      { datasetNames, rulers, segmentGroupNames: [], petLayer: true },
      zip,
      session
    );
  });
});
