import { APIRequestContext, expect } from '@playwright/test';
import { CONFIG, apiUrl } from './config';
import { readJson } from './http';
import { expectJob } from './limits';

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

export type RegisteredTask = { id: string; title: string; dockerImage: string };

export async function listTasks(
  request: APIRequestContext,
  token: string,
  folderId: string
): Promise<RegisteredTask[]> {
  const res = await request.get(apiUrl(`/folder/${folderId}/volview_processing/tasks`), {
    headers: { 'Girder-Token': token },
  });
  return readJson(res, 'list tasks');
}

export type TaskOption = { title: string; occurrence: number };

const RADIOLOGY_IMAGE = process.env.CLI_IMAGE || 'volview-radiology-cli:latest';
const EXAMPLES_IMAGE = process.env.CLI_EXAMPLES_IMAGE || 'volview-cli-examples:latest';

function taskOption(tasks: RegisteredTask[], task: RegisteredTask): TaskOption {
  return {
    title: task.title,
    occurrence: tasks.filter(({ title }) => title === task.title).findIndex(({ id }) => id === task.id),
  };
}

function imageTask(
  tasks: RegisteredTask[], image: string, titlePrefix: string
): RegisteredTask {
  const matches = tasks.filter(({ title, dockerImage }) =>
    dockerImage === image && title.startsWith(titlePrefix)
  );
  expect(matches, `expected one "${titlePrefix}*" task from ${image}`).toHaveLength(1);
  return matches[0];
}

export async function expectTaskImages(
  request: APIRequestContext, g: { token: string; folderId: string }
): Promise<void> {
  expect(EXAMPLES_IMAGE).not.toBe(RADIOLOGY_IMAGE);
  const tasks = await listTasks(request, g.token, g.folderId);
  const titles = (image: string) => tasks
    .filter(({ dockerImage }) => dockerImage === image).map(({ title }) => title).sort();
  expect(titles(RADIOLOGY_IMAGE)).toEqual([
    'Export Segment Statistics', 'Generate Segment Rulers',
  ]);
  expect(titles(EXAMPLES_IMAGE)).toEqual([
    'Masked Median Filter', 'Median Filter', 'Otsu Multi-level Segmentation', 'Threshold Segmentation',
  ]);
}

export async function segmentTaskOptions(
  request: APIRequestContext, g: { token: string; folderId: string }
): Promise<{ rulers: TaskOption; report: TaskOption }> {
  const tasks = await listTasks(request, g.token, g.folderId);
  return {
    rulers: taskOption(tasks, imageTask(tasks, RADIOLOGY_IMAGE, 'Generate Segment Rulers')),
    report: taskOption(tasks, imageTask(tasks, RADIOLOGY_IMAGE, 'Export Segment Statistics')),
  };
}

export async function exampleTaskOption(
  request: APIRequestContext, g: { token: string; folderId: string }, titlePrefix: string
): Promise<TaskOption> {
  const tasks = await listTasks(request, g.token, g.folderId);
  return taskOption(tasks, imageTask(tasks, EXAMPLES_IMAGE, titlePrefix));
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

// Poll a job to a terminal state (success/error/cancelled).
export async function pollJob(
  request: APIRequestContext,
  token: string,
  jobId: string
): Promise<{ state: string; [k: string]: unknown }> {
  let last: any;
  await expectJob
    .poll(
      async () => {
        const res = await request.get(apiUrl(`/volview_processing/jobs/${jobId}`), {
          headers: { 'Girder-Token': token },
        });
        last = await readJson(res, `poll job ${jobId}`);
        return TERMINAL.has(last?.state) ? 'terminal' : last?.state;
      },
      { message: `[e2e] job ${jobId} did not reach a terminal state` }
    )
    .toBe('terminal');
  return last;
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
  const tasks = await listTasks(request, token, folderId);
  const task = imageTask(tasks, EXAMPLES_IMAGE, 'Otsu');
  const jobId = await runTask(request, token, folderId, task.id, {
    inputVolume: { type: 'image', uris },
    numberOfLevels: 3,
  });
  const final = await pollJob(request, token, jobId);
  return { jobId, state: final.state };
}
