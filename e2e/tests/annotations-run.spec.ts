import { execFileSync } from 'child_process';
import { mkdtemp, readFile, readdir, rm } from 'fs/promises';
import { tmpdir } from 'os';
import { join } from 'path';
import { gunzipSync } from 'zlib';
import { test, expect, Locator, Page } from '@playwright/test';
import { expectJob } from '../helpers/limits';
import { segmentTaskOptions, TaskOption } from '../helpers/jobs';
import { setupFixture, plantCookie, requireHarnessState, Girder } from '../helpers/girder';
import { apiUrl } from '../helpers/config';
import { makeNrrd } from '../helpers/nrrd';
import { createFolderUnder, uploadFile } from '../helpers/provision';
import { gotoFolder, checkRowByItemId, checkRowByTexts, openInVolView } from '../helpers/girder-ui';
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
  addSegment,
  placeRuler,
  paintStrokes,
  readRulerMeasurements,
  readSegmentNames,
  rulerMeasurementRows,
} from '../helpers/annotations';

async function launchChecked(driver: Page, g: Girder): Promise<Page> {
  await gotoFolder(driver, g.folderId);
  await checkRowByItemId(driver, g.itemId);
  const launch = await openInVolView(driver);
  await waitForVolViewReady(launch.popup);
  return launch.popup;
}

async function addNamedSegment(view: Page, name: string): Promise<void> {
  await addSegment(view);
  await view.locator('[data-testid="segment-list"] [data-testid="segment-color-button"]').last().click();
  const editor = view.getByRole('dialog');
  await editor.getByRole('textbox', { name: /^Name\b/ }).fill(name);
  await editor.getByRole('button', { name: 'Done', exact: true }).click();
  await expect(editor).toBeHidden();
  expect(await readSegmentNames(view)).toContain(name);
}

async function submitSegmentJob(view: Page, task: TaskOption): Promise<Locator> {
  await selectTask(view, task.title, task.occurrence);
  await waitForInputBound(view);
  const rows = view.locator('.job-row').filter({ has: view.locator('.job-title', { hasText: task.title }) });
  const before = await rows.count();
  await submitTaskFromForm(view);
  await expect(rows, `${task.title} did not appear in job history`).toHaveCount(before + 1);
  const row = rows.first();
  await row.getByRole('button', { name: 'Details', exact: true }).click();
  await expectJob(row.locator('.job-subtitle'), `${task.title} did not succeed`).toContainText(/^Succeeded\b/);
  return row;
}

async function downloadReport(view: Page, row: Locator): Promise<Record<string, string>[]> {
  const files = row.locator('.result-file-name');
  await expect(files, 'the report job must return only a CSV').toHaveCount(1);
  await expect(files).toHaveText(/\.csv$/);
  await expect(row.getByRole('button', { name: 'Load', exact: true })).toHaveCount(0);
  const downloadReady = view.waitForEvent('download');
  await row.locator('button.result-file').click();
  const download = await downloadReady;
  expect(download.suggestedFilename()).toMatch(/\.csv$/);
  const file = await download.path();
  if (!file) throw new Error('The report download has no local file');
  const lines = (await readFile(file, 'utf8')).trimEnd().split(/\r?\n/);
  const cells = (line: string) =>
    Array.from(line.matchAll(/(?:^|,)(?:"((?:[^"]|"")*)"|([^,]*))/g), (match) =>
      match[1] === undefined ? match[2] : match[1].replace(/""/g, '"')
    );
  const headers = cells(lines[0]);
  return lines.slice(1).map((line) =>
    Object.fromEntries(cells(line).map((value, index) => [headers[index], value]))
  );
}

async function expectedSegmentStatistics(
  view: Page, g: Girder, labelmapUri: string
): Promise<Record<string, number>> {
  const response = await view.request.get(new URL(labelmapUri, view.url()).href, {
    headers: { 'Girder-Token': g.token },
  });
  expect(response.ok(), 'could not download the staged segment').toBeTruthy();
  const file = await response.body();
  const headerEnd = file.indexOf('\n\n');
  expect(headerEnd).toBeGreaterThan(0);
  const header = file.subarray(0, headerEnd).toString('ascii');
  expect(header).toMatch(/^type: unsigned char$/m);
  expect(header).toMatch(/^sizes: 16 16 16$/m);
  expect(header).toMatch(/^encoding: gzip$/m);
  expect(header).toMatch(/^space directions: \(1,0,0\) \(0,1,0\) \(0,0,1\)$/m);
  expect(header).toMatch(/^space origin: \(0,0,0\)$/m);
  const mask = gunzipSync(file.subarray(headerEnd + 2));
  expect(mask).toHaveLength(16 ** 3);
  expect(new Set(mask)).toEqual(new Set([0, 1]));

  // Read the fixture's image samples at the voxels actually painted in the UI.
  const image = makeNrrd();
  const pixels = image.subarray(image.indexOf('\n\n') + 2);
  const samples = Array.from(mask).flatMap((label, index) =>
    label === 1 ? [pixels.readInt16LE(index * 2)] : []
  ).sort((a, b) => a - b);
  const count = samples.length;
  expect(count).toBeGreaterThan(1);
  const sum = samples.reduce((total, value) => total + value, 0);
  const mean = sum / count;
  return {
    volume_mm3: count,
    intensity_min: samples[0],
    intensity_max: samples[count - 1],
    intensity_mean: mean,
    intensity_median: (samples[Math.floor((count - 1) / 2)] + samples[Math.floor(count / 2)]) / 2,
    intensity_stddev: Math.sqrt(samples.reduce((total, value) => total + (value - mean) ** 2, 0) / count),
    intensity_sum: sum,
  };
}

function expectSegmentStatistics(row: Record<string, string>, expected: Record<string, number>): void {
  for (const [column, value] of Object.entries(expected)) {
    expect(row[column], `${column} is missing from the CSV`).not.toBe('');
    expect(Number(row[column]), column).toBeCloseTo(value, 6);
  }
}

const rulerLengths = (measurements: { lengthText: string }[]): number[] =>
  measurements.map(({ lengthText }) => Number(lengthText.replace('mm', '')));

const segmentColumns = [
  'input_image_path', 'segment_name', 'labelmap_index', 'volume_mm3', 'warnings',
  'intensity_min', 'intensity_max', 'intensity_mean', 'intensity_median',
  'intensity_stddev', 'intensity_sum',
  'pet_intensity_min', 'pet_intensity_max', 'pet_intensity_mean',
  'pet_intensity_median', 'pet_intensity_stddev', 'pet_intensity_sum',
];

const petColumns = segmentColumns.filter((column) => column.startsWith('pet_'));

async function makeDicomReportFixture(directory: string): Promise<void> {
  // Both grids cover [0, 15] mm on each axis. Every CT sample is 100 and
  // every PET sample is 8 after applying the DICOM rescale tags.
  execFileSync('uv', ['run', '--no-project', '--with', 'pydicom', 'python', '-c', `
from pathlib import Path
import sys
from pydicom import Dataset, FileMetaDataset, dcmwrite
from pydicom.uid import (
    CTImageStorage, ExplicitVRLittleEndian,
    PositronEmissionTomographyImageStorage, generate_uid,
)

study, frame = generate_uid(), generate_uid()
for modality, count, spacing, raw, slope, intercept, sop in [
    ('CT', 16, 1, 1124, 1, -1024, CTImageStorage),
    ('PT', 6, 3, 32, 0.25, 0, PositronEmissionTomographyImageStorage),
]:
    series = generate_uid()
    for z in range(count):
        data = Dataset()
        data.file_meta = FileMetaDataset()
        data.file_meta.MediaStorageSOPClassUID = sop
        data.file_meta.MediaStorageSOPInstanceUID = generate_uid()
        data.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        data.SOPClassUID = sop
        data.SOPInstanceUID = data.file_meta.MediaStorageSOPInstanceUID
        data.StudyInstanceUID, data.SeriesInstanceUID = study, series
        data.FrameOfReferenceUID = frame
        data.PatientID, data.PatientName = 'REPORT-PET', 'Report^PET'
        data.StudyDate, data.StudyTime = '20200101', '120000'
        data.StudyDescription = 'Report CT and PET'
        data.Modality = modality
        data.SeriesDescription = 'Report CT' if modality == 'CT' else 'Report PET'
        data.SeriesNumber, data.InstanceNumber = (1 if modality == 'CT' else 2), z + 1
        data.Rows = data.Columns = count
        data.PixelSpacing, data.SliceThickness = [spacing, spacing], spacing
        data.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        data.ImagePositionPatient = [0, 0, z * spacing]
        data.SamplesPerPixel, data.PhotometricInterpretation = 1, 'MONOCHROME2'
        data.BitsAllocated = data.BitsStored = 16
        data.HighBit, data.PixelRepresentation = 15, 1
        data.RescaleSlope, data.RescaleIntercept = slope, intercept
        if modality == 'PT':
            data.Units = 'BQML'
        data.PixelData = raw.to_bytes(2, 'little', signed=True) * count * count
        destination = Path(sys.argv[1]) / f'{modality}-{count - z:02d}.dcm'
        dcmwrite(destination, data, enforce_file_format=True)
`, directory], { stdio: ['ignore', 'ignore', 'inherit'] });
}

function expectReportedRulers(
  row: Record<string, string>, lengths: number[], maxRulers = lengths.length
): void {
  const sorted = [...lengths].sort((a, b) => b - a);
  const rulerColumns = Array.from({ length: maxRulers }, (_, i) => `ruler_${i + 1}_length_mm`);
  expect(Object.keys(row)).toEqual([...segmentColumns, ...rulerColumns]);
  expect(rulerColumns.map((column, i) =>
    i < sorted.length ? Number(row[column]).toFixed(2) : row[column]
  )).toEqual(rulerColumns.map((_, i) => i < sorted.length ? sorted[i].toFixed(2) : ''));
}

test.describe('Segment rulers and statistics reports', () => {
  test('stages a placed ruler as the annotations input and applies the generated rulers', async ({
    page,
    context,
  }, info) => {
    const g = await setupFixture(context, 'jobs-annotations');
    const tasks = await segmentTaskOptions(context.request, g);
    const view = await launchChecked(page, g);

    await paintStrokes(view);
    const paintedNames = await readSegmentNames(view);
    expect(paintedNames).toHaveLength(1);

    // A ruler on an unpainted segment leaves the painted region unmeasured.
    const orphanName = `${paintedNames[0]} LD`;
    await addNamedSegment(view, orphanName);
    await placeRuler(view, [-40, -25], [40, 25]);
    expect((await readRulerMeasurements(view)).length, 'the ruler was not placed').toBe(1);

    await selectTask(view, tasks.rulers.title, tasks.rulers.occurrence);
    await waitForInputBound(view);
    // The annotations input binds every finished tool on the active image.
    await expect(
      view
        .locator('.jobs-module')
        .getByText(/1 annotation on /)
        .first(),
      'the placed ruler was not bound as the annotations input'
    ).toBeVisible();

    const submission = view.waitForRequest((request) =>
      request.method() === 'POST' &&
      /\/volview_processing\/tasks\/[^/]+\/run$/.test(new URL(request.url()).pathname)
    );
    await submitTaskFromForm(view);
    await waitForJobComplete(view);

    // The long- and short-axis rulers land alongside the source ruler: the
    // result is additive, so the staged annotation is not echoed back.
    await expect(
      await rulerMeasurementRows(view),
      'the annotations result did not add the generated rulers to the image'
    ).toHaveCount(3);
    const { inputLabelmap } = (await submission).postDataJSON().values;
    expect(inputLabelmap.uris).toHaveLength(1);
    const staged = await view.request.get(new URL(inputLabelmap.uris[0], view.url()).href, {
      headers: { 'Girder-Token': g.token },
    });
    expect(staged.ok()).toBeTruthy();
    const bytes = await staged.body();
    const header = bytes.subarray(0, bytes.indexOf('\n\n')).toString('ascii');
    const stagedNames = header.split('\n')
      .filter((line) => /^Segment\d+_Name:=/.test(line))
      .map((line) => line.split(':=')[1]);
    expect(stagedNames).toEqual(paintedNames);
    const rulers = await rulerMeasurementRows(view);
    await expect(rulers.getByText(orphanName, { exact: true })).toHaveCount(1);
    await expect(rulers.getByText(paintedNames[0], { exact: true })).toHaveCount(2);
    await shot(view, info, 'annotations-live-apply');
  });

  test('blocks submission until a segmentation is painted', async ({ page, context }) => {
    const g = await setupFixture(context, 'jobs-annotations-blocked');
    const tasks = await segmentTaskOptions(context.request, g);
    const view = await launchChecked(page, g);

    await selectTask(view, tasks.rulers.title, tasks.rulers.occurrence);
    // The image input still binds to the active dataset; only the label map
    // input is unsatisfiable, and it fails closed rather than staging nothing.
    await waitForInputBound(view);

    const panel = view.locator('.jobs-module');
    const submit = panel.getByRole('button', { name: 'Submit', exact: true });
    await expect(
      panel.getByText('Paint a segment on the current image first.').first(),
      'no unbound-labelmap message in the form'
    ).toBeVisible();
    await expect(submit, 'Submit was enabled with nothing painted').toBeDisabled();

    // Painting one rebinds the form.
    await paintStrokes(view);
    await openModuleTab(view, 'Jobs');
    await expect(submit, 'painting did not rebind the label map input').toBeEnabled();
  });

  test('reports an unmeasured segment, generates rulers, then exports their measurements', async ({
    page,
    context,
  }, info) => {
    const g = await setupFixture(context, 'jobs-segment-rulers');
    const tasks = await segmentTaskOptions(context.request, g);
    const view = await launchChecked(page, g);

    await paintStrokes(view);
    expect(await readSegmentNames(view)).not.toEqual([]);
    await expect(
      await rulerMeasurementRows(view),
      'the test must begin without rulers'
    ).toHaveCount(0);

    const namesBefore = await readSegmentNames(view);
    const submission = view.waitForRequest((request) =>
      request.method() === 'POST' &&
      /\/volview_processing\/tasks\/[^/]+\/run$/.test(new URL(request.url()).pathname)
    );
    const unmeasuredJob = await submitSegmentJob(view, tasks.report);
    const unmeasured = await downloadReport(view, unmeasuredJob);
    const { inputLabelmap } = (await submission).postDataJSON().values;
    expect(inputLabelmap.uris).toHaveLength(1);
    const expectedStatistics = await expectedSegmentStatistics(view, g, inputLabelmap.uris[0]);
    expect(unmeasured).toHaveLength(1);
    expectSegmentStatistics(unmeasured[0], expectedStatistics);
    expect(unmeasured[0].segment_name).toBe(namesBefore[0]);
    expectReportedRulers(unmeasured[0], []);
    expect(petColumns.map((column) => unmeasured[0][column])).toEqual(petColumns.map(() => ''));
    expect(Number(unmeasured[0].volume_mm3)).toBeGreaterThan(0);
    expect(unmeasured[0].warnings).toContain('No rulers are assigned');
    await expect(await rulerMeasurementRows(view)).toHaveCount(0);

    await selectTask(view, tasks.rulers.title, tasks.rulers.occurrence);
    await waitForInputBound(view);
    const panel = view.locator('.jobs-module');
    await expect(
      panel.getByText('Segmentation on active dataset').first(),
      'the painted segment labelmaps were not bound'
    ).toBeVisible();
    await expect(panel.getByText('Annotations (optional)').first()).toBeVisible();
    await expect(panel.getByText('Not provided', { exact: true }).first()).toBeVisible();
    await expect(
      panel.getByRole('button', { name: 'Submit', exact: true }),
      'optional annotations still blocked submission'
    ).toBeEnabled();
    await shot(view, info, 'segment-rulers-optional-input-form');

    const generatedJob = await submitSegmentJob(view, tasks.rulers);
    await expect(generatedJob.getByRole('button', { name: 'Load', exact: true })).toHaveCount(0);
    await expect(generatedJob.locator('.result-file-name')).toHaveCount(0);

    await expect(
      await rulerMeasurementRows(view),
      'the generated long- and short-axis rulers were not applied'
    ).toHaveCount(2);
    await shot(view, info, 'segment-rulers-generated-without-input-annotations');
    const generated = await readRulerMeasurements(view);
    await expect((await rulerMeasurementRows(view)).locator('.v-list-item-title'))
      .toHaveText([namesBefore[0], namesBefore[0]]);
    const generatedNames = await readSegmentNames(view);
    expect(generatedNames).toEqual(namesBefore);
    const reportJob = await submitSegmentJob(view, tasks.report);
    const report = await downloadReport(view, reportJob);
    expect(report).toHaveLength(1);
    expect(report[0].segment_name).toBe(namesBefore[0]);
    expectReportedRulers(report[0], rulerLengths(generated));
    expect(report[0].warnings).toBe('');
    expectSegmentStatistics(report[0], expectedStatistics);
    expect(await readRulerMeasurements(view)).toEqual(generated);
    expect(await readSegmentNames(view)).toEqual(generatedNames);
    await shot(view, info, 'segment-generated-measurements-report');

    const rerun = await submitSegmentJob(view, tasks.rulers);
    await expect(rerun.getByRole('button', { name: 'Load', exact: true })).toHaveCount(0);
    await expect(await rulerMeasurementRows(view)).toHaveCount(2);
    expect(await readRulerMeasurements(view)).toEqual(generated);
    expect(await readSegmentNames(view)).toEqual(namesBefore);
  });

  test('sizes ruler columns to the largest segment group and preserves manual rulers', async ({
    page,
    context,
  }, info) => {
    const g = await setupFixture(context, 'jobs-segment-statistics');
    const tasks = await segmentTaskOptions(context.request, g);
    const view = await launchChecked(page, g);
    await paintStrokes(view);
    const namesBefore = await readSegmentNames(view);
    expect(namesBefore).toHaveLength(1);

    await placeRuler(view, [0, -20], [0, 20]);
    await placeRuler(view, [-50, 0], [50, 0]);
    const rows = await rulerMeasurementRows(view);
    await expect(rows).toHaveCount(2);
    await expect(rows.locator('.v-list-item-title')).toHaveText([namesBefore[0], namesBefore[0]]);
    const manual = await readRulerMeasurements(view);
    const lengths = rulerLengths(manual);
    expect(lengths[0]).toBeLessThan(lengths[1]);

    await placeRuler(view, [-20, -10], [20, 10]);
    const threeRulers = await readRulerMeasurements(view);
    expect(threeRulers).toHaveLength(3);

    // A ruler on an unpainted segment still contributes a separate report row.
    const orphanName = `${namesBefore[0]} LD`;
    await addNamedSegment(view, orphanName);
    const namesWithOrphan = await readSegmentNames(view);
    expect(namesWithOrphan).toHaveLength(2);
    await placeRuler(view, [-15, -30], [15, -30]);
    const allRulers = await readRulerMeasurements(view);
    expect(allRulers).toHaveLength(4);
    const expandedJob = await submitSegmentJob(view, tasks.report);
    const expandedReport = await downloadReport(view, expandedJob);
    expect(expandedReport).toHaveLength(2);
    const segment = expandedReport.find(({ segment_name }) => segment_name === namesBefore[0]);
    const orphan = expandedReport.find(({ segment_name }) => segment_name === orphanName);
    expect(segment).toBeDefined();
    expect(orphan).toBeDefined();
    expectReportedRulers(segment!, rulerLengths(threeRulers), 3);
    expect(segment!.warnings).toBe('');
    expectReportedRulers(orphan!, rulerLengths(allRulers.slice(3)), 3);
    expect(await readRulerMeasurements(view)).toEqual(allRulers);
    expect(await readSegmentNames(view)).toEqual(namesWithOrphan);
    await shot(view, info, 'segment-numbered-rulers-report');

    const generatedJob = await submitSegmentJob(view, tasks.rulers);
    await expect(generatedJob.getByRole('button', { name: 'Load', exact: true })).toHaveCount(0);
    await expect(generatedJob.getByText(/\boutputs? unavailable\b/)).toHaveCount(0);
    await expect(generatedJob.locator('.result-file-name')).toHaveCount(0);
    expect(await readRulerMeasurements(view)).toEqual(allRulers);
    expect(await readSegmentNames(view)).toEqual(namesWithOrphan);
    await shot(view, info, 'segment-manual-rulers-preserved');
  });

  test('reports CT and its PET layer when both inputs are multi-file DICOM series', async ({
    page, context,
  }, info) => {
    const state = requireHarnessState();
    await plantCookie(context, state.token);
    const folderId = await createFolderUnder(
      context.request, state.token, 'folder', state.runRootFolderId, 'jobs-report-dicom'
    );
    const dataFolderId = await createFolderUnder(
      context.request, state.token, 'folder', folderId, 'dicom'
    );
    const directory = await mkdtemp(join(tmpdir(), 'segment-report-dicom-'));
    try {
      await makeDicomReportFixture(directory);
      const headers = { 'Girder-Token': state.token };
      const key = 'large_image.auto_set';
      const setting = await context.request.get(apiUrl('/system/setting'), {
        headers, params: { key },
      });
      expect(setting.ok(), 'could not read the DICOM auto-conversion setting').toBeTruthy();
      const previous = await setting.json();
      const setAutoSet = async (value: unknown) => {
        const response = await context.request.put(apiUrl('/system/setting'), {
          headers, params: { key, value: JSON.stringify(value) },
        });
        expect(response.ok(), 'could not update the DICOM auto-conversion setting').toBeTruthy();
      };
      try {
        await setAutoSet(false);
        for (const filename of (await readdir(directory)).sort()) {
          await uploadFile(
            context.request, state.token, dataFolderId, filename,
            await readFile(join(directory, filename))
          );
        }
      } finally {
        await setAutoSet(previous);
      }
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
    await uploadFile(
      context.request, state.token, folderId, '.large_image_config.yaml',
      await readFile(join(__dirname, '..', 'fixtures', 'dicom.large_image_config.yaml'))
    );

    await gotoFolder(page, folderId);
    await checkRowByTexts(page, ['REPORT-PET', 'Report CT']);
    await checkRowByTexts(page, ['REPORT-PET', 'Report PET']);
    const { popup: view } = await openInVolView(page);
    await waitForVolViewReady(view);
    await paintStrokes(view);
    const names = await readSegmentNames(view);
    expect(names).toHaveLength(1);
    const tasks = await segmentTaskOptions(context.request, { token: state.token, folderId });
    await selectTask(view, tasks.report.title, tasks.report.occurrence);
    const panel = view.locator('.jobs-module');
    const ctInput = panel.locator('.file-widget').filter({
      has: view.getByText('Input Volume', { exact: true }),
    });
    await expect(ctInput).toContainText('Active dataset');
    await expect(ctInput).toContainText('Report CT');
    const petInput = panel.locator('.file-widget').filter({
      has: view.getByText('PET Volume (optional)', { exact: true }),
    });
    await expect(petInput).toContainText('Image layer 1');
    await expect(petInput).toContainText('Report PET');

    const submission = view.waitForRequest((request) =>
      request.method() === 'POST' &&
      /\/volview_processing\/tasks\/[^/]+\/run$/.test(new URL(request.url()).pathname)
    );
    const job = await submitSegmentJob(view, tasks.report);
    const { inputVolume, inputPETVolume } = (await submission).postDataJSON().values;
    expect(inputVolume.uris).toHaveLength(16);
    expect(inputPETVolume.uris).toHaveLength(6);
    expect(inputPETVolume.uris.some((uri: string) => inputVolume.uris.includes(uri))).toBeFalsy();
    const rows = await downloadReport(view, job);
    expect(rows).toHaveLength(1);
    const row = rows[0];
    expect(row.segment_name).toBe(names[0]);
    expectReportedRulers(row, []);
    const voxelCount = Number(row.volume_mm3);
    expect(voxelCount).toBeGreaterThan(0);
    expect(row.warnings).not.toContain('outside');
    for (const [prefix, value] of [['', 100], ['pet_', 8]] as const) {
      expectSegmentStatistics(row, {
        [`${prefix}intensity_min`]: value,
        [`${prefix}intensity_max`]: value,
        [`${prefix}intensity_mean`]: value,
        [`${prefix}intensity_median`]: value,
        [`${prefix}intensity_stddev`]: 0,
        [`${prefix}intensity_sum`]: value * voxelCount,
      });
    }
    await shot(view, info, 'segment-report-ct-pet-dicom');
  });
});
