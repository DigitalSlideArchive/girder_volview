import { Page, expect } from '@playwright/test';
import { openModuleTab } from './volview';
import { RulerRecord } from './compat-state';

// Content creation + readback inside VolView, selector-compatible with both the
// main-era client (capture) and the branch client (verify) — grounded on main's
// ControlsStripTools / AnnotationsModule / SegmentGroupControls /
// PatientStudyVolumeBrowser markup, which the branch retains.

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

async function activateTool(page: Page, icon: 'mdi-ruler' | 'mdi-brush'): Promise<void> {
  const button = page.locator(`button:has(i.${icon})`).first();
  await expect(button, `no ${icon} tool button`).toBeVisible();
  await button.click();
}

// Two clicks at center±40px on the first 2D view — the same gesture VolView's
// own wdio suite uses to place a ruler.
export async function placeRuler(page: Page): Promise<void> {
  await activateTool(page, 'mdi-ruler');
  const box = await first2DCanvasBox(page);
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.click(cx - 40, cy);
  await page.waitForTimeout(300);
  await page.mouse.click(cx + 40, cy);
  await page.waitForTimeout(500);
}

const openAnnotationsTab = async (page: Page, tabText: 'Measurements' | 'Segment Groups') => {
  await openModuleTab(page, 'Annotations');
  const tab = page.locator('.v-tab', { hasText: tabText }).first();
  await expect(tab, `no "${tabText}" tab in the Annotations module`).toBeVisible();
  await tab.click();
};

// Ruler rows in the Measurements list; the rendered length ("40.00mm") comes
// from world coordinates in the session, so restores must reproduce it exactly.
export async function readRulerMeasurements(page: Page): Promise<RulerRecord[]> {
  await openAnnotationsTab(page, 'Measurements');
  const rows = page.locator('.v-list-item:has(i.tool-icon.mdi-ruler)');
  await expect(rows.first(), 'no ruler row in the Measurements list').toBeVisible();
  const texts = await rows.allTextContents();
  return texts
    .map((t) => t.match(/\d+\.\d{2}\s*mm/)?.[0]?.replace(/\s+/, ''))
    .filter((t): t is string => !!t)
    .map((lengthText) => ({ lengthText }));
}

// Paint a few strokes on the first 2D view. Activating paint (and stroking)
// auto-creates a segment group for the current image when none exists.
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

export async function readSegmentGroupNames(page: Page): Promise<string[]> {
  await openAnnotationsTab(page, 'Segment Groups');
  const names = page.locator('.segment-group-list .group-name');
  await expect(names.first(), 'no segment group listed').toBeVisible();
  return (await names.allTextContents()).map((t) => t.trim()).filter(Boolean);
}

const dicomVolumeCard = (page: Page, seriesDescription: string) =>
  page.locator('.v-card', { has: page.locator('.series-desc') }).filter({ hasText: seriesDescription }).first();

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
      { timeout: 60_000, message: 'layer never finished loading' }
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
