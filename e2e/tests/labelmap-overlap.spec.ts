import { test, expect, Page } from '@playwright/test';
import { setupFixture, Girder } from '../helpers/girder';
import { gotoFolder, checkRowByItemId, openInVolView } from '../helpers/girder-ui';
import {
  waitForVolViewReady,
  selectTask,
  waitForInputBound,
  shot,
} from '../helpers/volview';
import {
  paintStrokes,
  addSegment,
  lockSegment,
  readSegmentNames,
} from '../helpers/annotations';

// A labelmap file gives each voxel one value, so two segments claiming the same
// voxel cannot ship in one. A task that takes a single labelmap therefore leaves
// whole segments behind, and the form says which; a task that takes many ships
// them all and says nothing.

async function launchChecked(driver: Page, g: Girder): Promise<Page> {
  await gotoFolder(driver, g.folderId);
  await checkRowByItemId(driver, g.itemId);
  const launch = await openInVolView(driver);
  await waitForVolViewReady(launch.popup);
  return launch.popup;
}

test.describe('overlapping segments staged as a labelmap input', () => {
  test('names the segments a single-labelmap input omits', async ({ page, context }, info) => {
    const g = await setupFixture(context, 'jobs-overlap');
    const view = await launchChecked(page, g);

    // The same strokes twice, under two segments. Painting takes the voxels it
    // covers from every unlocked segment, so the first is locked before the
    // second is painted: the two masks then hold identical voxels and cannot be
    // flattened together.
    await paintStrokes(view);
    const [first] = await readSegmentNames(view);
    await lockSegment(view, first);
    await addSegment(view);
    await paintStrokes(view);
    expect(
      (await readSegmentNames(view)).length,
      'the overlap needs two painted segments'
    ).toBeGreaterThan(1);

    const notice = view.locator('.jobs-module [data-testid="staging-omission-notice"]');

    await selectTask(view, 'MaskedMedianFilter');
    await waitForInputBound(view);
    await expect(
      notice.first(),
      'the singular labelmap input did not report the segments it leaves out'
    ).toBeVisible();
    await shot(view, info, 'labelmap-omission-notice');

    // RegionOfInterestRulers takes many labelmaps, so the same overlap ships
    // whole and the notice goes away.
    await selectTask(view, 'RegionOfInterestRulers');
    await waitForInputBound(view);
    await expect(
      view.locator('.jobs-module').getByText('Segmentation on active dataset').first(),
      'the painted segmentation was not bound to the multi-labelmap input'
    ).toBeVisible();
    await expect(notice, 'a multi-labelmap input omitted a segment').toHaveCount(0);
    await shot(view, info, 'labelmap-no-omission-notice');
  });
});
