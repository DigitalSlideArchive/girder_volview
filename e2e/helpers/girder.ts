import { APIRequestContext, BrowserContext, expect } from '@playwright/test';
import { CONFIG, apiUrl } from './config';
import {
  CompatState,
  FixtureId,
  readCompatState,
  requireFixture,
} from './compat-state';

export { CONFIG };

export type Girder = {
  token: string;
  folderId: string;
  itemId: string;
  itemName: string;
  itemIds: string[];
  itemNames: string[];
};

const api = apiUrl;

export async function plantCookie(context: BrowserContext, token: string) {
  const { hostname } = new URL(CONFIG.baseURL);
  await context.addCookies([
    { name: 'girderToken', value: token, domain: hostname, path: '/' },
  ]);
}

export function requireHarnessState(): CompatState {
  const state = readCompatState();
  if (!state?.provisioned) {
    throw new Error('[e2e] no harness state — run through e2e/scripts/compat.sh');
  }
  return state;
}

export async function setupFixture(context: BrowserContext, id: FixtureId): Promise<Girder> {
  const state = requireHarnessState();
  const fixture = requireFixture(state, id);
  await plantCookie(context, state.token);
  return {
    token: state.token,
    folderId: fixture.folderId,
    itemId: fixture.itemIds[0],
    itemName: fixture.itemNames[0],
    itemIds: fixture.itemIds,
    itemNames: fixture.itemNames,
  };
}

// The session.volview.zip items currently in a folder. countSessionItems proves
// a save created a NEW session item; the compat capture diffs the listing to
// discover which item a save minted (main's save response carries no resumeUrl).
export async function listSessionItems(
  request: APIRequestContext,
  token: string,
  folderId: string
): Promise<Array<{ _id: string; name: string }>> {
  const res = await request.get(api(`/item?folderId=${folderId}&limit=1000`), {
    headers: { 'Girder-Token': token },
  });
  const items: Array<{ _id: string; name: string }> = await res.json();
  // Substring, not endsWith: girder dedupes colliding item names by appending
  // " (1)", and the backend's isSessionItem treats those as sessions too.
  return items.filter((it) => it.name.includes('.volview.zip'));
}

export async function countSessionItems(request: APIRequestContext, g: Girder): Promise<number> {
  return (await listSessionItems(request, g.token, g.folderId)).length;
}

// The id of an item's first file. Two saves collide on file NAME (girder
// dedupes the item name, "session.volview.zip (1)", while the file inside keeps
// the original), so the file id is what distinguishes one save from another in
// a manifest — resources carry it in their minted /file/<id>/proxiable URL.
export async function firstFileId(
  request: APIRequestContext,
  token: string,
  itemId: string
): Promise<string> {
  const res = await request.get(api(`/item/${itemId}/files?limit=1`), {
    headers: { 'Girder-Token': token },
  });
  expect(res.ok(), `GET /item/${itemId}/files returned HTTP ${res.status()}`).toBeTruthy();
  const files: Array<{ _id: string }> = await res.json();
  expect(files?.[0]?._id, `item ${itemId} has no files`).toBeTruthy();
  return files[0]._id;
}

export const resourceUrls = (json: any): string[] =>
  Array.isArray(json?.resources) ? json.resources.map((r: any) => r?.url).filter(Boolean) : [];

// Fetch a manifest by the `urls=` leg a launched tab is carrying. Used to
// inspect WHICH resources a launch resolved to without racing the tab's own
// in-flight request (a popup can finish loading before an interceptor attaches).
export async function fetchManifest(
  request: APIRequestContext,
  token: string,
  urls: string
): Promise<any> {
  const res = await request.get(`${CONFIG.baseURL}${urls}`, {
    headers: { 'Girder-Token': token },
  });
  expect(res.ok(), `manifest ${urls} returned HTTP ${res.status()}`).toBeTruthy();
  return res.json();
}
