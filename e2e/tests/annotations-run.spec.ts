import { test, expect, Page } from '@playwright/test';
import { setupFixture, Girder } from '../helpers/girder';
import { gotoFolder, checkRowByItemId, openInVolView } from '../helpers/girder-ui';
import {
  waitForVolViewReady,
  openModuleTab,
  selectTask,
  waitForInputBound,
  submitTaskFromForm,
  waitForJobComplete,
  shot,
} from '../helpers/volview';
import {
  placeRuler,
  paintStrokes,
  readRulerMeasurements,
  readSegmentGroupNames,
  rectangleMeasurementRows,
  rulerMeasurementRows,
} from '../helpers/annotations';

// RulerToRectangle proves annotation input/output by returning one rectangle
// per ruler without echoing the additive source annotations.

async function launchChecked(driver: Page, g: Girder): Promise<Page> {
  await gotoFolder(driver, g.folderId);
  await checkRowByItemId(driver, g.itemId);
  const launch = await openInVolView(driver);
  await waitForVolViewReady(launch.popup);
  return launch.popup;
}

test.describe('vector annotations through a job', () => {
  test('stages a placed ruler and applies the derived rectangle', async ({
    page,
    context,
  }, info) => {
    const g = await setupFixture(context, 'jobs-annotations');
    const view = await launchChecked(page, g);

    // Nothing derived yet: the rectangle row below can only come from the job.
    await expect(await rectangleMeasurementRows(view)).toHaveCount(0);

    // A diagonal gives the native rectangle nonzero extent on both image axes.
    await placeRuler(view, [-40, -25], [40, 25]);
    expect((await readRulerMeasurements(view)).length, 'the ruler was not placed').toBe(1);

    await selectTask(view, 'RulerToRectangle');
    await waitForInputBound(view);
    // The annotations input binds to every finished tool on the active image —
    // no picker, and the count is what identifies the bound value.
    await expect(
      view
        .locator('.jobs-module')
        .getByText(/1 annotation on /)
        .first(),
      'the placed ruler was not bound as the annotations input'
    ).toBeVisible();

    await submitTaskFromForm(view);
    await waitForJobComplete(view);

    // COUNT, not content: the rectangle is derived from a canvas gesture.
    await expect(
      await rectangleMeasurementRows(view),
      'the annotations result did not add a rectangle to the image'
    ).toHaveCount(1, { timeout: 30_000 });
    expect(
      (await readRulerMeasurements(view)).length,
      'applying the annotations result duplicated the source ruler'
    ).toBe(1);
    await shot(view, info, 'annotations-live-apply');
  });

  test('blocks submission until an annotation is placed', async ({ page, context }) => {
    const g = await setupFixture(context, 'jobs-annotations-blocked');
    const view = await launchChecked(page, g);

    await selectTask(view, 'RulerToRectangle');
    // The image input still binds to the active dataset; only the annotations
    // input is unsatisfiable, and it fails closed rather than staging nothing.
    await waitForInputBound(view);

    const panel = view.locator('.jobs-module');
    const submit = panel.getByRole('button', { name: 'Submit', exact: true });
    await expect(
      panel.getByText('Place a ruler, rectangle, or polygon on the current image first.').first(),
      'no unbound-annotations message in the form'
    ).toBeVisible();
    await expect(submit, 'Submit was enabled with no annotations placed').toBeDisabled();

    // Placing one rebinds the form.
    await placeRuler(view);
    await openModuleTab(view, 'Jobs');
    await expect(submit, 'placing a ruler did not rebind the annotations input').toBeEnabled();
  });

  test('runs an optional annotations input empty and applies generated ROI rulers', async ({
    page,
    context,
  }, info) => {
    const g = await setupFixture(context, 'jobs-roi-rulers');
    const view = await launchChecked(page, g);

    await paintStrokes(view);
    expect(await readSegmentGroupNames(view)).not.toEqual([]);
    await expect(
      await rulerMeasurementRows(view),
      'the test must begin without rulers'
    ).toHaveCount(0);

    await selectTask(view, 'RegionOfInterestRulers');
    await waitForInputBound(view);
    const panel = view.locator('.jobs-module');
    await expect(
      panel.getByText('Active segment group').first(),
      'the painted ROI labelmap was not bound'
    ).toBeVisible();
    await expect(panel.getByText('Annotations (optional)').first()).toBeVisible();
    await expect(panel.getByText('Not provided', { exact: true }).first()).toBeVisible();
    await expect(
      panel.getByRole('button', { name: 'Submit', exact: true }),
      'optional annotations still blocked submission'
    ).toBeEnabled();
    await shot(view, info, 'roi-rulers-optional-input-form');

    await submitTaskFromForm(view);
    await waitForJobComplete(view);

    await expect(
      await rulerMeasurementRows(view),
      'the generated long- and short-axis rulers were not applied'
    ).toHaveCount(2, { timeout: 30_000 });
    await shot(view, info, 'roi-rulers-generated-without-input-annotations');
  });
});
