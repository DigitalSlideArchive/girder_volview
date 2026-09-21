import { Locator, Page, expect } from '@playwright/test';
import { openModuleTab } from './volview';
import { RulerRecord } from './compat-state';

// Content creation + readback inside VolView, selector-compatible with both the
// main-era client (capture) and the branch client (verify/current), grounded on
// ControlsStripTools / AnnotationsModule / MeasurementsToolList /
// PatientStudyVolumeBrowser markup. The segment surface is the one place the
// two generations diverge: main nests segments under segment groups and splits
// the Annotations module into tabs, the branch lists segments directly.

async function first2DCanvasBox(page: Page) {
  const canvas = page
    .locator('div[data-testid~="vtk-two-view"] canvas, div[data-testid~="vtk-cine-view"] canvas')
    .first();
  await expect(canvas, 'no 2D view canvas').toBeVisible();
  const box = await canvas.boundingBox();
  if (!box || box.width < 50 || box.height < 50) {
    throw new Error(`2D view canvas has no usable size: ${JSON.stringify(box)}`);
  }
  return box;
}

// The branch client's Annotations section headers carry the same icons as the
// tools, and come first in the page, so the lookup stays inside the tools strip.
async function activateTool(page: Page, icon: 'mdi-ruler' | 'mdi-brush'): Promise<void> {
  const button = page.locator(`#tools-strip button:has(i.${icon})`).first();
  await expect(button, `no ${icon} tool button`).toBeVisible();
  await button.click();
}

// The default matches VolView's wdio ruler gesture; callers may choose offsets.
export async function placeRuler(
  page: Page,
  firstOffset: readonly [number, number] = [-40, 0],
  secondOffset: readonly [number, number] = [40, 0]
): Promise<void> {
  await activateTool(page, 'mdi-ruler');
  const box = await first2DCanvasBox(page);
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.click(cx + firstOffset[0], cy + firstOffset[1]);
  await page.waitForTimeout(300);
  await page.mouse.click(cx + secondOffset[0], cy + secondOffset[1]);
  await page.waitForTimeout(500);
}

// Structural markers of the two client generations, both rendered as soon as
// the Annotations module exists: main always builds the tab strip, the branch
// never does. Content is not a tell, since an empty segment list looks the
// same in both.
const LEGACY_SEGMENT_GROUP_TAB = '.v-tab:has-text("Segment Groups")';
const BRANCH_SEGMENT_LIST = '[data-testid="segment-list"]';

async function openAnnotations(page: Page): Promise<'legacy' | 'branch'> {
  await openModuleTab(page, 'Annotations');
  await expect(
    page.locator(`${LEGACY_SEGMENT_GROUP_TAB}, ${BRANCH_SEGMENT_LIST}`).first(),
    'neither client generation rendered an Annotations module'
  ).toBeAttached();
  return (await page.locator(LEGACY_SEGMENT_GROUP_TAB).count()) > 0 ? 'legacy' : 'branch';
}

// Measurements are a tab in the main-era client and a collapsible section, open
// by default, in the branch client.
async function openMeasurements(page: Page): Promise<void> {
  if ((await openAnnotations(page)) === 'branch') {
    const section = page.locator('[data-testid="measurements-section"]');
    if ((await section.getAttribute('aria-expanded')) !== 'true') await section.click();
    return;
  }
  const tab = page.locator('.v-tab', { hasText: 'Measurements' }).first();
  await expect(tab, 'no "Measurements" tab in the Annotations module').toBeVisible();
  await tab.click();
}

// Measurement rows key off the shape icon in both generations; the class
// carrying it is the only thing that moved.
const measurementRows = (page: Page, icon: string): Locator =>
  page.locator(
    `.v-list-item:has(i.tool-icon.${icon}), ` +
      `[data-testid="segment-shape-row"]:has(i.shape-icon.${icon})`
  );

// Ruler rows in the Measurements list; the rendered length ("40.00mm") comes
// from world coordinates in the session, so restores must reproduce it exactly.
export async function rulerMeasurementRows(page: Page): Promise<Locator> {
  await openMeasurements(page);
  return measurementRows(page, 'mdi-ruler');
}

export async function readRulerMeasurements(page: Page): Promise<RulerRecord[]> {
  const rows = await rulerMeasurementRows(page);
  await expect(rows.first(), 'no ruler row in the Measurements list').toBeVisible();
  const texts = await rows.allTextContents();
  return texts
    .map((t) => t.match(/\d+\.\d{2}\s*mm/)?.[0]?.replace(/\s+/, ''))
    .filter((t): t is string => !!t)
    .map((lengthText) => ({ lengthText }));
}

// Rectangle rows, returned as a locator so callers can await a count.
export async function rectangleMeasurementRows(page: Page): Promise<Locator> {
  await openMeasurements(page);
  return measurementRows(page, 'mdi-vector-square');
}

// Paint a few strokes on the first 2D view. Activating paint mints and selects
// a segment for the current image when the scene holds none.
export async function paintStrokes(page: Page): Promise<void> {
  await activateTool(page, 'mdi-brush');
  await page.waitForTimeout(500);
  const box = await first2DCanvasBox(page);
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  for (const [dx, dy] of [
    [-30, -20],
    [-10, 15],
  ]) {
    await page.mouse.move(cx + dx, cy + dy);
    await page.mouse.down();
    await page.mouse.move(cx + dx + 40, cy + dy + 10, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(300);
  }
}

const trimmed = (texts: string[]): string[] =>
  [...new Set(texts.map((t) => t.trim()).filter(Boolean))];

// The main-era client shows only the selected group's segments, so every group
// has to be selected in turn; the class pair pins the segment chip list against
// the identically shaped label chip list in the tool controls above it.
const LEGACY_SEGMENT_CHIPS = '.v-item-group.my-4 .v-chip:has(.dot-container) .text-truncate';

// Segment names on the current image. Group names are deliberately not read:
// they do not exist in the branch client, where a restored group's segments
// land in the image's one segmentation.
export async function readSegmentNames(page: Page): Promise<string[]> {
  if ((await openAnnotations(page)) === 'legacy') {
    const groups = page.locator('.segment-group-list .group-name');
    await expect(groups.first(), 'no segment group listed').toBeVisible();
    const chips = page.locator(LEGACY_SEGMENT_CHIPS);
    const groupCount = await groups.count();
    const names: string[] = [];
    for (let i = 0; i < groupCount; i += 1) {
      await groups.nth(i).click();
      await expect(chips.first(), 'the selected segment group lists no segment').toBeVisible();
      names.push(...(await chips.allTextContents()));
    }
    return trimmed(names);
  }
  const names = page.locator(`${BRANCH_SEGMENT_LIST} .segment-items .item-row .v-list-item-title`);
  await expect(names.first(), 'no segment listed').toBeVisible();
  return trimmed(await names.allTextContents());
}

// Rows in the branch client's segment list, for counting what a job result or a
// stroke added.
export const segmentRows = (page: Page): Locator =>
  page.locator(`${BRANCH_SEGMENT_LIST} .segment-items .item-row`);

// Lock a segment so strokes on another one share its voxels instead of taking
// them: painting clears what it covers from every unlocked segment. Branch-only.
export async function lockSegment(page: Page, name: string): Promise<void> {
  await openAnnotations(page);
  const list = page.locator(BRANCH_SEGMENT_LIST);
  await list.getByRole('button', { name: `Lock ${name}`, exact: true }).click();
  await expect(
    list.getByRole('button', { name: `Unlock ${name}`, exact: true }),
    `${name} did not lock`
  ).toBeVisible();
}

// Mint a second segment and make it the paint target (the branch client selects
// what it creates). Branch-only: the main-era client has no such list.
export async function addSegment(page: Page): Promise<void> {
  await openAnnotations(page);
  const before = await segmentRows(page).count();
  const create = page.locator(`${BRANCH_SEGMENT_LIST} .create-row`).first();
  await expect(create, 'no "New segment" row in the segment list').toBeVisible();
  await create.click();
  await expect(segmentRows(page), 'the new segment did not appear').toHaveCount(before + 1);
}

const dicomVolumeCard = (page: Page, seriesDescription: string) =>
  page
    .locator('.v-card', { has: page.locator('.series-desc') })
    .filter({ hasText: seriesDescription })
    .first();

export async function readDatasetNames(page: Page): Promise<string[]> {
  await openModuleTab(page, 'Data');
  // DICOM volumes render series-desc cards; non-DICOM images render name rows.
  const dicomNames = await page.locator('.series-desc .text-ellipsis').allTextContents();
  const imageNames = await page
    .locator('.text-body-2.font-weight-bold.text-no-wrap.text-truncate')
    .allTextContents();
  return [...dicomNames, ...imageNames].map((t) => t.trim()).filter(Boolean);
}

// Make the volume with this series description the PRIMARY selection (layer
// targets attach to the primary).
export async function selectPrimaryVolume(page: Page, seriesDescription: string): Promise<void> {
  await openModuleTab(page, 'Data');
  const card = dicomVolumeCard(page, seriesDescription);
  await expect(card, `no volume card for "${seriesDescription}"`).toBeVisible();
  await card.click();
  await page.waitForTimeout(1_000);
}

async function openDatasetMenu(page: Page, seriesDescription: string) {
  await openModuleTab(page, 'Data');
  const card = dicomVolumeCard(page, seriesDescription);
  await expect(card, `no volume card for "${seriesDescription}"`).toBeVisible();
  await card.locator('[data-testid="dataset-menu-button"]').click();
  return page.locator('.v-overlay-container [data-testid="dataset-menu-layer-item"]').first();
}

// Ensure the volume is layered onto the primary. VolView auto-layers PET over
// CT when a whole CT+PET study loads, so "already layered" is success, not an
// error.
export async function addLayer(page: Page, seriesDescription: string): Promise<void> {
  const layerItem = await openDatasetMenu(page, seriesDescription);
  await expect(layerItem, `no layer menu item for "${seriesDescription}"`).toBeVisible();
  const initial = (await layerItem.textContent().catch(() => '')) || '';
  if (initial.includes('Remove as layer')) {
    await page.keyboard.press('Escape');
    return;
  }
  await layerItem.click();
  // Layer load is async; poll the menu until it reads "Remove as layer".
  await expect
    .poll(
      async () => {
        await page.keyboard.press('Escape');
        await page.waitForTimeout(500);
        const item = await openDatasetMenu(page, seriesDescription);
        const text = (await item.textContent().catch(() => '')) || '';
        await page.keyboard.press('Escape');
        return text;
      },
      { message: 'layer never finished loading' }
    )
    .toContain('Remove as layer');
  await page.keyboard.press('Escape');
}

export async function isLayered(page: Page, seriesDescription: string): Promise<boolean> {
  const layerItem = await openDatasetMenu(page, seriesDescription);
  const text = (await layerItem.textContent().catch(() => '')) || '';
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  return text.includes('Remove as layer');
}
