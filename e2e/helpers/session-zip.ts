import AdmZip from 'adm-zip';
import { APIRequestContext } from '@playwright/test';
import { apiUrl } from './config';
import { readJson } from './http';
import { ZipSummary } from './compat-state';

// Semantic summary of a saved session.volview.zip: counts and presence pulled
// from the manifest.json inside the archive (schema keys per VolView's
// io/state-file/schema.ts: tools.rulers.tools, the segment masks, and
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

// What a labelmap contributes to the summary, independent of the manifest
// generation it was read from.
type MaskFacts = {
  segmentNames: string[];
  maskCount: number;
  // Archive entry names holding the labelmap voxels.
  maskPaths: string[];
};

const texts = (values: unknown[]): string[] =>
  values.filter((v): v is string => typeof v === 'string' && v.trim().length > 0);

// 6.x kept segment identity inside each segment group's metadata; 7.0.0 lifted
// it into a top-level `segments` registry that per-image masks point back into.
// One summary has to read both, because capture writes the older shape.
const legacyMaskFacts = (groups: any[]): MaskFacts => {
  const descriptorsOf = (group: any): any[] =>
    Object.values(group?.metadata?.segments?.byValue ?? {});
  return {
    segmentNames: texts(groups.flatMap((g) => descriptorsOf(g).map((d) => d?.name))),
    // A group that declared no descriptors still restores as at least one mask:
    // its labelmap values are enumerated when the voxels are read.
    maskCount: groups.reduce((n, g) => n + Math.max(1, descriptorsOf(g).length), 0),
    maskPaths: texts(groups.map((g) => g?.path)),
  };
};

const currentMaskFacts = (manifest: any): MaskFacts => {
  const nameById = new Map<string, unknown>(
    (manifest.segments ?? []).map((s: any) => [s?.id, s?.name])
  );
  const masks: any[] = (manifest.segmentations ?? []).flatMap((s: any) => s?.masks ?? []);
  return {
    segmentNames: texts(masks.map((m) => nameById.get(m?.segmentId))),
    maskCount: masks.length,
    maskPaths: texts(masks.map((m) => m?.representations?.labelmap?.path)),
  };
};

const maskFacts = (manifest: any): MaskFacts =>
  Array.isArray(manifest.segmentGroups)
    ? legacyMaskFacts(manifest.segmentGroups)
    : currentMaskFacts(manifest);

const largestEntryBytes = (zip: AdmZip, paths: string[]): number =>
  zip
    .getEntries()
    .filter((e) => paths.some((p) => e.entryName === p || e.entryName.startsWith(`${p}/`)))
    .reduce((max, e) => Math.max(max, e.header.size), 0);

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
  const masks = maskFacts(manifest);

  return {
    rulerCount: manifest.tools?.rulers?.tools?.length ?? 0,
    maskCount: masks.maskCount,
    maskDataBytes: largestEntryBytes(zip, masks.maskPaths),
    segmentNames: masks.segmentNames,
    hasLayers: (manifest.parentToLayers?.length ?? 0) > 0,
    version: manifest.version,
  };
}
