import { test, expect } from '@playwright/test';
import { setupFixture, requireHarnessState } from '../../helpers/girder';
import { apiUrl } from '../../helpers/config';
import { readJson } from '../../helpers/http';
import { gotoFolder, checkRowByItemId, openInVolView } from '../../helpers/girder-ui';
import { waitForVolViewReady, openModuleTab, loadJobResults, shot } from '../../helpers/volview';
import { readSegmentNames, segmentRows } from '../../helpers/annotations';

// A job the baseline ran must still load after the upgrade. Result intents are
// projected from the stored output specs on request, so the branch answers for
// the baseline's job with the current intent name. The branch client also reads
// the legacy name, which is why the backend's answer is asserted on its own
// before the result is loaded.
test.describe('compat verify: a baseline job', () => {
  test('jobs-baseline: Load results applies the labelmap the baseline produced', async ({
    page,
    context,
    request,
  }, info) => {
    const state = requireHarnessState();
    if (!state.baselineJob) throw new Error('[compat] capture did not record a baseline job');
    const { jobId, segmentNames } = state.baselineJob;

    const res = await request.get(apiUrl(`/volview_processing/jobs/${jobId}/results`), {
      headers: { 'Girder-Token': state.token },
    });
    const { intents } = await readJson(res, `results of baseline job ${jobId}`);
    const segmentation = intents.find((i: any) => i.intent === 'import-segmentation');
    expect(
      segmentation,
      `the baseline job has no import-segmentation result: ${JSON.stringify(intents)}`
    ).toBeTruthy();
    expect(segmentation.source.jobId).toBe(jobId);

    const g = await setupFixture(context, 'jobs-baseline');
    await gotoFolder(page, g.folderId);
    await checkRowByItemId(page, g.itemId);
    const { popup: view } = await openInVolView(page);
    await waitForVolViewReady(view);

    await openModuleTab(view, 'Annotations');
    await expect(segmentRows(view), 'the baseline job applied without "Load"').toHaveCount(0);

    // The segment names travel inside the result's .seg.nrrd, so both clients
    // must list the same ones. readSegmentNames drops duplicates, so the row
    // count is what catches a result imported twice.
    await loadJobResults(view);
    expect((await readSegmentNames(view)).sort()).toEqual([...segmentNames].sort());
    await expect(segmentRows(view)).toHaveCount(segmentNames.length);
    await shot(view, info, 'verify-baseline-job-applied');
  });
});
