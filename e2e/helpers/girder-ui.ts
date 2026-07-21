import { Page, expect } from '@playwright/test';
import { CONFIG } from './config';
import { isManifestGet, requireManifestJson } from './manifest';

// Drivers for the real Girder web UI: the large_image item list's filter box
// and checkboxes, and the plugin's Open-in-VolView affordances. All browser
// scenarios launch through these product paths, so open.js always builds the
// URL under test.

// A VolView popup plus the valid manifest its boot fetched.
export type VolViewLaunch = { popup: Page; manifest: Promise<any> };

async function watchManifest(popup: Page): Promise<any> {
  const response = await popup.waitForResponse(isManifestGet, { timeout: 90_000 });
  return requireManifestJson(response);
}

async function toLaunch(popupPromise: Promise<Page>): Promise<VolViewLaunch> {
  const popup = await popupPromise;
  // Attach BEFORE the app boots far enough to fetch the manifest — the popup
  // event fires at window creation, megabytes of app JS before any fetch.
  const manifest = watchManifest(popup);
  await popup.waitForLoadState('domcontentloaded');
  return { popup, manifest };
}

// Log the girder WEB CLIENT in through its UI. Planting the girderToken cookie
// authenticates VolView's cookie-accepting routes, but girder rejects a
// cookie-only token on state-changing requests (CSRF): main's folder-open path
// fires a metadata PUT before window.open, so without a real login that PUT
// 401s and the popup never opens. A UI login sets the client's currentToken, so
// its restRequest sends the Girder-Token header on writes.
export async function loginViaUI(page: Page): Promise<void> {
  await page.goto(`${CONFIG.baseURL}/#`, { waitUntil: 'domcontentloaded' });
  // DSA's "Log In" is an <a> with no href, so it is NOT a link-role element —
  // match it by tag + text. Absent → already authenticated.
  const loginLink = page.locator('a', { hasText: /log ?in/i }).first();
  await page.waitForLoadState('networkidle').catch(() => undefined);
  if (!(await loginLink.isVisible({ timeout: 10_000 }).catch(() => false))) {
    return;
  }
  await loginLink.click();
  await page.locator('#g-login').fill(CONFIG.user);
  await page.locator('#g-password').fill(CONFIG.pass);
  await page.locator('#g-login-button').click();
  await expect(
    page.locator('a', { hasText: /log ?in/i }).first(),
    'girder UI login did not complete (Log In link still present)'
  ).toBeHidden({ timeout: 30_000 });
}

// Navigate to a folder. When the girder SPA is already loaded (e.g. right after
// loginViaUI), change the hash IN-APP rather than page.goto — a full reload
// drops the client's in-memory auth token, and girder keeps no header-usable
// cookie, so the next write (main's pre-open metadata PUT) would 401.
export async function gotoFolder(page: Page, folderId: string): Promise<void> {
  if (page.url().startsWith(CONFIG.baseURL)) {
    await page.evaluate((id) => {
      window.location.hash = `#folder/${id}`;
    }, folderId);
  } else {
    await page.goto(`${CONFIG.baseURL}/#folder/${folderId}`, { waitUntil: 'domcontentloaded' });
  }
  await expect(page.locator('li.g-item-list-entry').first()).toBeVisible({ timeout: 30_000 });
}

export async function checkRowByItemId(page: Page, itemId: string): Promise<void> {
  const row = page.locator(`li.g-item-list-entry:has(a[href="#item/${itemId}"])`);
  await expect(row, `no item-list row for item ${itemId}`).toBeVisible();
  await row.locator('input.g-list-checkbox').check();
}

// Clear every checked row. gotoFolder changes the hash IN-APP, so checkbox
// state survives navigation — a "bare folder-open" right after a checked
// gesture would otherwise still carry the earlier selection.
export async function uncheckAllRows(page: Page): Promise<void> {
  const checked = () => page.locator('input.g-list-checkbox:checked');
  // Bounded: each uncheck removes one from the set.
  for (let guard = 0; guard < 50 && (await checked().count()) > 0; guard += 1) {
    await checked().first().uncheck();
  }
  await expect(checked(), 'rows remained checked').toHaveCount(0);
}

// Grouped item lists render metadata columns as cell text; match a row by the
// conjunction of distinctive cell values (e.g. PatientID + SeriesDescription).
export function rowByTexts(page: Page, texts: string[]) {
  return texts.reduce(
    (rows, text) => rows.filter({ hasText: text }),
    page.locator('li.g-item-list-entry')
  );
}

export async function checkRowByTexts(page: Page, texts: string[]): Promise<void> {
  const row = rowByTexts(page, texts).first();
  await expect(row, `no item-list row containing ${JSON.stringify(texts)}`).toBeVisible();
  await row.locator('input.g-list-checkbox').check();
}

// large_image's item-list filter box narrows the listing server-side.
export async function fillFilterBox(page: Page, text: string): Promise<void> {
  const box = page.locator('input.li-item-list-filter-input').first();
  await expect(box, 'no large_image filter box — is .large_image_config.yaml applied?').toBeVisible();
  await box.fill(text);
  await box.press('Enter');
}

// Presence/absence of rows by cell-text conjunction. Count assertions are
// deliberately avoided: the .large_image_config.yaml item renders as its own
// (ungrouped) row, so absolute row counts are layout-dependent.
export async function expectRow(page: Page, texts: string[]): Promise<void> {
  await expect(rowByTexts(page, texts).first(), `expected a row containing ${JSON.stringify(texts)}`)
    .toBeVisible({ timeout: 30_000 });
}

export async function expectNoRow(page: Page, texts: string[]): Promise<void> {
  await expect
    .poll(async () => rowByTexts(page, texts).count(), {
      timeout: 30_000,
      message: `expected NO row containing ${JSON.stringify(texts)}`,
    })
    .toBe(0);
}

// Click the hierarchy header's Open-in-VolView button and hand back the popup.
// A "Will open newest VolView session" confirm modal may interpose (when a
// session item is among the checked rows).
export async function openInVolView(page: Page): Promise<VolViewLaunch> {
  const button = page.locator('.open-in-volview');
  await expect(button, 'Open-in-VolView button not visible').toBeVisible();
  const popupPromise = page.waitForEvent('popup', { timeout: 60_000 });
  await button.click();
  const modal = page.locator('.modal-content:has-text("Will open newest VolView session")');
  if (await modal.isVisible({ timeout: 2_000 }).catch(() => false)) {
    await page.locator('#g-confirm-button').click();
  }
  return toLaunch(popupPromise);
}

// Open from the item page (the single-item gesture).
export async function openFromItemPage(page: Page, itemId: string): Promise<VolViewLaunch> {
  await page.goto(`${CONFIG.baseURL}/#item/${itemId}`, { waitUntil: 'domcontentloaded' });
  const button = page.locator('.open-in-volview');
  await expect(button, 'no Open-in-VolView on the item page').toBeVisible();
  const popupPromise = page.waitForEvent('popup', { timeout: 60_000 });
  await button.click();
  return toLaunch(popupPromise);
}

// Drill through rows whose navigate is another item list (devkit patient →
// study); the LAST row's navigate opens VolView, so the final click yields the
// popup.
export async function drillRowNav(page: Page, rowTexts: string[]): Promise<VolViewLaunch> {
  for (const text of rowTexts.slice(0, -1)) {
    const row = rowByTexts(page, [text]).first();
    await expect(row, `no drill-down row containing "${text}"`).toBeVisible();
    await row.locator('a.g-item-list-link').first().click();
    await expect(page.locator('li.g-item-list-entry').first()).toBeVisible({ timeout: 30_000 });
  }
  const lastText = rowTexts[rowTexts.length - 1];
  const last = rowByTexts(page, [lastText]).first();
  await expect(last, `no final row containing "${lastText}"`).toBeVisible();
  const popupPromise = page.waitForEvent('popup', { timeout: 60_000 });
  await last.locator('a.g-item-list-link').first().click();
  return toLaunch(popupPromise);
}
