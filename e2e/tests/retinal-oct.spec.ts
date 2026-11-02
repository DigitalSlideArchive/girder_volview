import { createHash } from 'crypto';
import { readFileSync } from 'fs';
import * as path from 'path';
import { test as base, expect, Locator, Page } from '@playwright/test';
import { apiUrl } from '../helpers/config';
import { plantCookie, requireHarnessState } from '../helpers/girder';
import { gotoFolder, openInVolView } from '../helpers/girder-ui';
import { readJson } from '../helpers/http';
import { resourceNames } from '../helpers/manifest';
import { deleteFolder, uploadFile } from '../helpers/provision';
import { openModuleTab, shot } from '../helpers/volview';

const SEED = path.resolve(__dirname, '../seed');
const LAYOUTS = { 'Axial and En face': [['axial'], ['enface']] };
const LAYERS = [
  'Retinal nerve fiber layer (RNFL)',
  'Ganglion cell and inner plexiform layers (GCL+IPL)',
  'Inner nuclear layer (INL)',
  'Outer plexiform layer (OPL)',
  'Outer nuclear layer (ONL)',
  'Photoreceptor inner segments (IS)',
  'Photoreceptor outer segments (OS)',
  'Retinal pigment epithelium (RPE)',
];

type RetinalFolder = { token: string; folderId: string };
type SampleFile = { name: string; bytes: number; sha256: string };

const test = base.extend<{ retinalFolder: RetinalFolder }>({
  retinalFolder: async ({ request, context }, use) => {
    const { files } = JSON.parse(
      readFileSync(path.join(SEED, 'manifest/retinal-oct.json'), 'utf8')
    ) as { files: SampleFile[] };
    const uploads = files.map((file) => {
      const bytes = readFileSync(path.join(SEED, 'data/developer/retinal-oct', file.name));
      expect(bytes.length, `${file.name} has the wrong size`).toBe(file.bytes);
      expect(createHash('sha256').update(bytes).digest('hex'), `${file.name} checksum`)
        .toBe(file.sha256);
      return { name: file.name, bytes };
    });
    const { token, runRootFolderId } = requireHarnessState();
    const response = await request.post(apiUrl('/folder'), {
      headers: { 'Girder-Token': token },
      params: {
        parentType: 'folder', parentId: runRootFolderId,
        name: 'retinal-oct', reuseExisting: false, public: false,
      },
    });
    const folder = await readJson(response, 'create retinal fixture folder');
    const folderId = folder._id as string;
    expect(folderId, 'retinal fixture folder has no id').toBeTruthy();
    try {
      expect(folder.public).toBe(false);
      for (const file of uploads) {
        await uploadFile(request, token, folderId, file.name, file.bytes);
      }
      await uploadFile(
        request, token, folderId, '.volview_config.yaml',
        readFileSync(path.join(SEED, 'configs/retinal-oct/.volview_config.yaml'))
      );
      await plantCookie(context, token);
      await use({ token, folderId });
    } finally {
      expect(await deleteFolder(request, token, folderId), 'retinal fixture cleanup failed')
        .toBe(true);
    }
  },
});

async function canvasPixels(canvas: Locator) {
  return canvas.evaluate((source: HTMLCanvasElement) => {
    const copy = document.createElement('canvas');
    copy.width = source.width;
    copy.height = source.height;
    const context = copy.getContext('2d');
    if (!context || !copy.width || !copy.height) return { visible: 0, colored: 0 };
    context.drawImage(source, 0, 0);
    const { data } = context.getImageData(0, 0, copy.width, copy.height);
    let visible = 0;
    let colored = 0;
    for (let offset = 0; offset < data.length; offset += 4) {
      const channels = [data[offset], data[offset + 1], data[offset + 2]];
      const maximum = Math.max(...channels);
      if (maximum > 20 && data[offset + 3] > 0) visible += 1;
      if (maximum - Math.min(...channels) > 40) colored += 1;
    }
    return { visible, colored };
  });
}

async function expectStackedViews(view: Page) {
  const axial = view.locator('[data-testid~="vtk-two-view"]');
  const enface = view.locator('[data-testid="enface-viewer"]');
  await expect(axial).toHaveCount(1);
  await expect(enface).toHaveCount(1);
  await expect.poll(() => view.locator('.view-type-select input')
    .evaluateAll((inputs) => inputs.map((input) => (input as HTMLInputElement).value)))
    .toEqual(['Axial', 'En face']);
  await expect(enface.locator('[data-testid="enface-projection-status"]'))
    .toHaveText('En face projection ready.');
  await expect.poll(async () => (await canvasPixels(axial.locator('canvas'))).visible)
    .toBeGreaterThan(1000);
  await expect.poll(async () => (await canvasPixels(enface.locator('[data-testid="enface-canvas"]'))).visible)
    .toBeGreaterThan(1000);
  const top = await axial.evaluate((element) => {
    const pane = element.closest('.vtk-container-wrapper');
    if (!pane) throw new Error('The Axial pane is missing');
    const { x, y, width, height } = pane.getBoundingClientRect();
    return { x, y, width, height };
  });
  const bottom = await enface.boundingBox();
  if (!top || !bottom) throw new Error('Retinal views have no visible layout bounds');
  expect(top.y + top.height, 'Axial must be above En face').toBeLessThanOrEqual(bottom.y + 2);
  expect(Math.abs(top.x - bottom.x), 'views must share their left edge').toBeLessThanOrEqual(2);
  expect(Math.abs(top.width - bottom.width), 'views must have the same width').toBeLessThanOrEqual(2);
}

async function expectImportedLayers(view: Page) {
  await openModuleTab(view, 'Annotations');
  const list = view.locator('[data-testid="segment-list"]');
  await expect(list.locator('.segment-items .item-row .v-list-item-title')).toHaveText(LAYERS);
  await expect(list).not.toHaveAttribute('aria-busy', 'true');
  for (const name of LAYERS) {
    await expect(view.getByRole('button', { name: `Reveal slice for ${name}`, exact: true }))
      .toBeEnabled();
  }
}

test('opens the retinal image and layers with Axial above En face', async ({
  page, request, retinalFolder,
}, info) => {
  const { folderId, token } = retinalFolder;
  const configResponse = await request.get(
    apiUrl(`/folder/${folderId}/volview_config/.volview_config.yaml`),
    { headers: { 'Girder-Token': token } }
  );
  const config = await readJson(configResponse, 'retinal folder configuration');
  expect(config.layouts).toEqual(LAYOUTS);
  expect(config).not.toHaveProperty('segments');
  expect(config).not.toHaveProperty('windowing');

  await gotoFolder(page, folderId);
  const launch = await openInVolView(page);
  try {
    expect(resourceNames(await launch.manifest).sort())
      .toEqual(['config.json', 'jhu-hc01.nrrd', 'jhu-hc01.seg.nrrd']);
    const view = launch.popup;
    await expectStackedViews(view);
    await shot(view, info, 'retinal-axial-and-enface');
    await openModuleTab(view, 'Data');
    await expect(view.locator('.image-list-card-active [title="jhu-hc01.nrrd"]'))
      .toBeVisible();
    await expectImportedLayers(view);
    await view.getByRole('button', { name: `Reveal slice for ${LAYERS[7]}`, exact: true }).click();
    await expect.poll(async () => (await canvasPixels(
      view.locator('[data-testid~="vtk-two-view"] canvas')
    )).colored).toBeGreaterThan(100);
    await expectStackedViews(view);
    await shot(view, info, 'retinal-eight-imported-layers');
  } finally {
    await launch.popup.close();
  }
});

test('highlights occupied retinal layer depth from the En face sidebar', async ({
  page, retinalFolder,
}, info) => {
  await gotoFolder(page, retinalFolder.folderId);
  const launch = await openInVolView(page);
  try {
    expect(resourceNames(await launch.manifest).sort())
      .toEqual(['config.json', 'jhu-hc01.nrrd', 'jhu-hc01.seg.nrrd']);
    const view = launch.popup;
    await expectStackedViews(view);
    await expectImportedLayers(view);
    await openModuleTab(view, 'Rendering');
    const sidebar = view.locator('[data-testid="enface-sidebar-controls"]');
    const section = sidebar.locator('[data-testid="enface-thickness-toggle"]');
    await expect(section).toHaveText('Thickness highlighting');
    await expect(section).toBeVisible();
    if ((await section.getAttribute('aria-expanded')) === 'false') await section.click();

    const picker = sidebar.locator('[data-testid="enface-segmentation-segment"]');
    await expect(picker.locator('input')).toBeEnabled();
    await picker.click();
    await expect.poll(async () => (await view.getByRole('option').allTextContents())
      .map((name) => name.trim()).sort()).toEqual([...LAYERS].sort());
    await view.getByRole('option', { name: LAYERS[7], exact: true }).click();
    await expect(picker).toContainText(LAYERS[7]);

    const highlight = sidebar.getByRole('checkbox', { name: 'Highlight thin regions', exact: true });
    const threshold = sidebar.locator('[data-testid="enface-thickness-threshold-input"] input');
    const slider = sidebar.locator('[data-testid="enface-thickness-threshold"] [role="slider"]');
    await expect(highlight).toBeEnabled();
    await expect(highlight).not.toBeChecked();
    await expect(threshold).toBeDisabled();
    await highlight.check();
    await expect(highlight).toBeChecked();
    const enface = view.locator('[data-testid="enface-viewer"]');
    await expect(enface).not.toHaveAttribute('aria-busy', 'true');
    await expect(threshold).toBeEnabled();
    await expect(slider).toBeEnabled();

    const canvas = enface.locator('[data-testid="enface-canvas"]');
    await threshold.fill('0');
    await expect(threshold).toHaveValue('0');
    await expect.poll(async () => (await canvasPixels(canvas)).colored).toBe(0);
    await threshold.fill('1000000');
    await expect.poll(async () => (await canvasPixels(canvas)).colored).toBeGreaterThan(100);
    const maximum = Number(await slider.getAttribute('aria-valuemax'));
    expect(maximum, 'the measured layer must have positive occupied depth').toBeGreaterThan(0);
    await slider.focus();
    await slider.press('Home');
    await expect(threshold).toHaveValue('0');
    await expect.poll(async () => (await canvasPixels(canvas)).colored).toBe(0);
    await slider.press('End');
    await expect.poll(async () => Number(await threshold.inputValue())).toBeCloseTo(maximum, 1);
    await expect.poll(async () => (await canvasPixels(canvas)).colored).toBeGreaterThan(100);
    await expectStackedViews(view);
    await shot(view, info, 'retinal-enface-thickness-highlighting');
    await highlight.uncheck();
    await expect.poll(async () => (await canvasPixels(canvas)).colored).toBe(0);
    await expect(threshold).toBeDisabled();
  } finally {
    await launch.popup.close();
  }
});
