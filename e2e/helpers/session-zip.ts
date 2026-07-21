import AdmZip from 'adm-zip';
import { APIRequestContext } from '@playwright/test';
import { apiUrl } from './config';
import { readJson } from './http';
import { ZipSummary } from './compat-state';

// Semantic summary of a saved session.volview.zip: counts and presence pulled
// from the manifest.json inside the archive (schema keys per VolView's
// io/state-file/schema.ts — tools.rulers.tools, segmentGroups[].path,
// parentToLayers). Deliberately NOT raw-JSON equality: the branch may migrate
// the schema, and that must stay a non-failure.

// The newest *.volview.zip FILE inside an item (item-scoped saves append the
// session zip beside the image file; session items hold exactly one).
async function newestSessionFile(
  request: APIRequestContext,
  token: string,
  itemId: string
): Promise<{ _id: string; name: string }> {
  const res = await request.get(apiUrl(`/item/${itemId}/files?limit=100`), {
    headers: { 'Girder-Token': token },
  });
  const files: Array<{ _id: string; name: string; created: string }> = await readJson(
    res,
    `list files of item ${itemId}`
  );
  const zips = files
    .filter((f) => f.name.endsWith('.volview.zip'))
    .sort((a, b) => new Date(b.created).getTime() - new Date(a.created).getTime());
  if (!zips.length) throw new Error(`[compat] item ${itemId} holds no *.volview.zip file`);
  return zips[0];
}

export async function fetchZipSummary(
  request: APIRequestContext,
  token: string,
  itemId: string
): Promise<ZipSummary> {
  const file = await newestSessionFile(request, token, itemId);
  const res = await request.get(apiUrl(`/file/${file._id}/download`), {
    headers: { 'Girder-Token': token },
  });
  if (res.status() >= 300) {
    throw new Error(`[compat] download of ${file.name} failed: HTTP ${res.status()}`);
  }
  const zip = new AdmZip(await res.body());
  const manifestEntry = zip.getEntry('manifest.json');
  if (!manifestEntry) {
    throw new Error(`[compat] ${file.name} has no manifest.json (not a VolView session zip?)`);
  }
  const manifest = JSON.parse(manifestEntry.getData().toString('utf8'));

  const segmentGroups: Array<{ path?: string }> = manifest.segmentGroups ?? [];
  const groupPaths = new Set(segmentGroups.map((g) => g.path).filter(Boolean));
  const segmentGroupDataBytes = zip
    .getEntries()
    .filter((e) => [...groupPaths].some((p) => e.entryName === p || e.entryName.startsWith(`${p}/`)))
    .reduce((max, e) => Math.max(max, e.header.size), 0);

  return {
    rulerCount: manifest.tools?.rulers?.tools?.length ?? 0,
    segmentGroupCount: segmentGroups.length,
    segmentGroupDataBytes,
    hasLayers: (manifest.parentToLayers?.length ?? 0) > 0,
    version: manifest.version,
  };
}
