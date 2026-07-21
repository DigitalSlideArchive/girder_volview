import { Page, expect } from '@playwright/test';
import { waitForVolViewReady } from './volview';

// Manifest interception shared by the lifecycle and compat suites: capture the
// GET /(item|folder)/:id/volview response a navigation triggers and classify
// whether it resumed a session (resources include a *.volview.zip) or loaded
// fresh raw images.

export const isSessionManifest = (json: any) =>
  Array.isArray(json?.resources) &&
  json.resources.some((r: any) => typeof r?.name === 'string' && r.name.endsWith('.volview.zip'));

export const resourceNames = (json: any): string[] =>
  Array.isArray(json?.resources) ? json.resources.map((r: any) => r?.name).filter(Boolean) : [];

export const isManifestGet = (response: { request: () => { method: () => string }; url: () => string }) =>
  response.request().method() === 'GET' &&
  /\/(item|folder)\/[^/]+\/volview$/.test(new URL(response.url()).pathname);

export async function captureManifest(page: Page, navigate: () => Promise<unknown>): Promise<any> {
  const manifestResp = page.waitForResponse(isManifestGet, { timeout: 60_000 });
  await navigate();
  const resp = await manifestResp.catch(() => undefined);
  // Status BEFORE the readiness wait, on purpose: a failed manifest means the
  // viewer never gets data, so waiting first turns a plain HTTP error into a
  // 90s "viewer never became ready" timeout that names nothing. Checked here
  // rather than in each caller so every launch and F5 gets it.
  if (resp) {
    expect(
      resp.ok(),
      `manifest ${new URL(resp.url()).pathname} returned HTTP ${resp.status()}`
    ).toBeTruthy();
  }
  await waitForVolViewReady(page);
  if (!resp) return undefined;
  try {
    return await resp.json();
  } catch {
    return undefined;
  }
}

export const gotoCapturingManifest = (page: Page, url: string) =>
  captureManifest(page, () => page.goto(url, { waitUntil: 'domcontentloaded' }));

export const reloadCapturingManifest = (page: Page) =>
  captureManifest(page, () => page.reload({ waitUntil: 'domcontentloaded' }));
