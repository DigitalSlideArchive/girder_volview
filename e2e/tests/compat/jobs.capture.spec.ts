import { test, expect } from '@playwright/test';
import { setupFixture } from '../../helpers/girder';
import { recordBaselineJob } from '../../helpers/compat-state';
import { gotoFolder, checkRowByItemId, openInVolView } from '../../helpers/girder-ui';
import {
  waitForVolViewReady,
  selectTask,
  waitForInputBound,
  submitTaskFromForm,
  waitForJobComplete,
  shot,
} from '../../helpers/volview';
import { readSegmentNames } from '../../helpers/annotations';

// The baseline client submits a labelmap job from its Jobs tab, so the job and
// its stored outputs are exactly what an upgrade inherits. The verify phase
// loads the same result through the branch client.
test.describe('compat capture: a finished job', () => {
  test('jobs-baseline: the baseline client runs Otsu and applies its labelmap', async ({
    page,
    context,
  }, info) => {
    const g = await setupFixture(context, 'jobs-baseline');
    await gotoFolder(page, g.folderId);
    await checkRowByItemId(page, g.itemId);
    const { popup: view } = await openInVolView(page);
    await waitForVolViewReady(view);

    await selectTask(view, 'Otsu');
    await waitForInputBound(view);
    const submitted = view.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        /\/volview_processing\/tasks\/[^/]+\/run$/.test(new URL(r.url()).pathname)
    );
    await submitTaskFromForm(view);
    const response = await submitted;
    expect(response.ok(), `the task run answered HTTP ${response.status()}`).toBeTruthy();
    const { jobId } = await response.json();
    expect(jobId, 'the task run answered with no jobId').toBeTruthy();

    await waitForJobComplete(view);
    const segmentNames = await readSegmentNames(view);
    expect(segmentNames, 'the baseline client applied no segment').not.toEqual([]);
    await shot(view, info, 'capture-baseline-job-applied');

    recordBaselineJob({ jobId, segmentNames });
  });
});
