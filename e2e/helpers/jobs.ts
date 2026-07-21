import { APIRequestContext } from '@playwright/test';
import { CONFIG, apiUrl } from './config';
import { readJson } from './http';

// Processing-job REST helpers — submit a task and poll it to a terminal state,
// mirroring the requests the browser client mints.

// The proxiable URI the backend resolves back to a Girder file id (and re-checks
// READ ACL): origin-relative /{apiRoot}/file/<id>/proxiable/<name>.
function proxiableUri(fileId: string, name: string): string {
  return `/${CONFIG.apiRoot}/file/${fileId}/proxiable/${encodeURIComponent(name)}`;
}

// The proxiable URIs for every file in an item (one per slice for a series, one
// for a single volume) — the CLI's <image> input binding.
export async function itemInputUris(
  request: APIRequestContext,
  token: string,
  itemId: string
): Promise<string[]> {
  const res = await request.get(apiUrl(`/item/${itemId}/files?limit=1000`), {
    headers: { 'Girder-Token': token },
  });
  const files: Array<{ _id: string; name: string }> = await readJson(res, `list files ${itemId}`);
  if (files.length === 0) throw new Error(`[e2e] item ${itemId} has no files to bind as input`);
  return files.map((f) => proxiableUri(f._id, f.name));
}

// Find a registered processing task by title prefix (e.g. 'Otsu'), scoped to the
// folder the run will target. Tasks come from the registered radiology CLI image.
export async function findTask(
  request: APIRequestContext,
  token: string,
  folderId: string,
  titlePrefix: string
): Promise<{ id: string; title: string }> {
  const res = await request.get(apiUrl(`/folder/${folderId}/volview_processing/tasks`), {
    headers: { 'Girder-Token': token },
  });
  const tasks: Array<{ id: string; title: string }> = await readJson(res, 'list tasks');
  const task = tasks.find((t) => (t.title || '').startsWith(titlePrefix));
  if (!task) {
    throw new Error(
      `[e2e] no "${titlePrefix}*" task registered — register the radiology CLI ` +
        `image with slicer_cli_web; tasks=${JSON.stringify(tasks.map((t) => t.title))}`
    );
  }
  return task;
}

// Submit a task run against a folder. `values` is the client-faithful body: the
// bound input + scalar params, NO output entries (the backend autofills them).
// Returns the created girder job id.
export async function runTask(
  request: APIRequestContext,
  token: string,
  folderId: string,
  taskId: string,
  values: Record<string, unknown>
): Promise<string> {
  const res = await request.post(apiUrl(`/folder/${folderId}/volview_processing/tasks/${taskId}/run`), {
    headers: { 'Girder-Token': token, 'Content-Type': 'application/json' },
    data: { values },
  });
  const body = await readJson(res, `run task ${taskId}`);
  const jobId = body?.jobId;
  if (!jobId) throw new Error(`[e2e] task run returned no jobId: ${JSON.stringify(body).slice(0, 300)}`);
  return jobId;
}

const TERMINAL = new Set(['success', 'error', 'cancelled']);

// Poll a job to a terminal state (success/error/cancelled) or throw on timeout.
export async function pollJob(
  request: APIRequestContext,
  token: string,
  jobId: string,
  timeoutMs = 240_000
): Promise<{ state: string; [k: string]: unknown }> {
  const deadline = Date.now() + timeoutMs;
  let last: any;
  while (Date.now() < deadline) {
    const res = await request.get(apiUrl(`/volview_processing/jobs/${jobId}`), {
      headers: { 'Girder-Token': token },
    });
    last = await readJson(res, `poll job ${jobId}`);
    if (TERMINAL.has(last?.state)) return last;
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error(`[e2e] job ${jobId} did not reach a terminal state within ${timeoutMs}ms (last=${last?.state})`);
}

// Submit an Otsu segmentation on the given item's image and poll to terminal.
// Returns { jobId, state }.
export async function submitOtsu(
  request: APIRequestContext,
  token: string,
  folderId: string,
  itemId: string
): Promise<{ jobId: string; state: string }> {
  const uris = await itemInputUris(request, token, itemId);
  const task = await findTask(request, token, folderId, 'Otsu');
  const jobId = await runTask(request, token, folderId, task.id, {
    inputVolume: { type: 'image', uris },
    numberOfLevels: 3,
  });
  const final = await pollJob(request, token, jobId);
  return { jobId, state: final.state };
}
