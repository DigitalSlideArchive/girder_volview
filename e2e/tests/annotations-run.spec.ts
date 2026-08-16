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
  rulerMeasurementRows,
} from '../helpers/annotations';

// RegionOfInterestRulers proves annotation input/output: it measures each
// painted segment group and returns only the rulers it generated, never
// echoing the source annotations it was given.

async function launchChecked(driver: Page, g: Girder): Promise<Page> {
  await gotoFolder(driver, g.folderId);
  await checkRowByItemId(driver, g.itemId);
  const launch = await openInVolView(driver);
  await waitForVolViewReady(launch.popup);
  return launch.popup;
}

test.describe('vector annotations through a job', () => {
  test('stages a placed ruler as the annotations input and applies the generated rulers', async ({
    page,
    context,
  }, info) => {
    const g = await setupFixture(context, 'jobs-annotations');
    const view = await launchChecked(page, g);

    // The measured region the job reports on.
    await paintStrokes(view);
    expect(await readSegmentGroupNames(view)).not.toEqual([]);

    // A diagonal ruler, carrying a name that does not parse as a generated
    // "<segment> LD"/"SD" label, so it never suppresses a measurement.
    await placeRuler(view, [-40, -25], [40, 25]);
    expect((await readRulerMeasurements(view)).length, 'the ruler was not placed').toBe(1);

    await selectTask(view, 'RegionOfInterestRulers');
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

    // The long- and short-axis rulers land alongside the source ruler: the
    // result is additive, so the staged annotation is not echoed back.
    await expect(
      await rulerMeasurementRows(view),
      'the annotations result did not add the generated rulers to the image'
    ).toHaveCount(3, { timeout: 30_000 });
    await shot(view, info, 'annotations-live-apply');
  });

  test('blocks submission until a segment group is painted', async ({ page, context }) => {
    const g = await setupFixture(context, 'jobs-annotations-blocked');
    const view = await launchChecked(page, g);

    await selectTask(view, 'RegionOfInterestRulers');
    // The image input still binds to the active dataset; only the label map
    // input is unsatisfiable, and it fails closed rather than staging nothing.
    await waitForInputBound(view);

    const panel = view.locator('.jobs-module');
    const submit = panel.getByRole('button', { name: 'Submit', exact: true });
    await expect(
      panel.getByText('Paint a segment group on the active dataset first.').first(),
      'no unbound-labelmap message in the form'
    ).toBeVisible();
    await expect(submit, 'Submit was enabled with no segment group painted').toBeDisabled();

    // Painting one rebinds the form.
    await paintStrokes(view);
    await openModuleTab(view, 'Jobs');
    await expect(submit, 'painting a segment group did not rebind the label map input').toBeEnabled();
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
      // RegionOfInterestRulers takes multiple label maps, so the widget names
      // the input plurally rather than as the single active segment group.
      panel.getByText('Segment groups on active dataset').first(),
      'the painted ROI labelmaps were not bound'
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
