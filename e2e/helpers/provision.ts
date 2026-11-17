import { APIRequestContext } from '@playwright/test';
import { CONFIG, apiUrl } from './config';
import { readJson } from './http';

// Data provisioning over the girder REST API: authenticate, create a fresh
// public folder, and upload two synthetic NRRD images, so the gestures have
// loadable distinct picks without operator-created fixtures.

// Basic-auth against girder; returns the session token + the user's _id.
export async function authenticate(
  request: APIRequestContext
): Promise<{ token: string; userId: string }> {
  const basic = Buffer.from(`${CONFIG.user}:${CONFIG.pass}`).toString('base64');
  const res = await request.get(apiUrl('/user/authentication'), {
    headers: { Authorization: `Basic ${basic}` },
  });
  const body = await readJson(res, `auth (${CONFIG.user}@${CONFIG.baseURL})`);
  const token = body?.authToken?.token;
  const userId = body?.user?._id;
  if (!token || !userId) {
    throw new Error('[e2e] auth response missing authToken.token or user._id');
  }
  return { token, userId };
}

// Create a public folder under any parent (user or folder).
export async function createFolderUnder(
  request: APIRequestContext,
  token: string,
  parentType: 'user' | 'folder',
  parentId: string,
  name: string
): Promise<string> {
  const url = apiUrl(
    `/folder?parentType=${parentType}&parentId=${parentId}` +
      `&name=${encodeURIComponent(name)}&reuseExisting=false&public=true`
  );
  const res = await request.post(url, { headers: { 'Girder-Token': token } });
  const folder = await readJson(res, `create folder ${name}`);
  if (!folder?._id) throw new Error('[e2e] folder create returned no _id');
  // eslint-disable-next-line no-console
  console.log(`[e2e] created folder ${folder._id} (${name})`);
  return folder._id;
}

// Upload one in-memory file to a folder via girder's two-step upload flow
// (init POST /file -> {_id}; then POST /file/chunk with the bytes as the raw
// request body). Modern girder REJECTS multipart on /file/chunk and reads the
// chunk from the body, with offset + uploadId in the query string (exactly what
// girder_client / the girder web client do). Girder auto-creates the containing
// item and returns the finalized File (with itemId).
export async function uploadFile(
  request: APIRequestContext,
  token: string,
  folderId: string,
  name: string,
  bytes: Buffer
): Promise<{ itemId: string; itemName: string }> {
  const initUrl = apiUrl(
    `/file?parentType=folder&parentId=${folderId}&name=${encodeURIComponent(name)}` +
      `&size=${bytes.length}&mimeType=application%2Foctet-stream`
  );
  const initRes = await request.post(initUrl, { headers: { 'Girder-Token': token } });
  const upload = await readJson(initRes, `init upload ${name}`);
  if (!upload?._id) throw new Error(`[e2e] init upload ${name} returned no _id`);

  const chunkUrl = apiUrl(`/file/chunk?offset=0&uploadId=${upload._id}`);
  const chunkRes = await request.post(chunkUrl, {
    headers: { 'Girder-Token': token, 'Content-Type': 'application/octet-stream' },
    data: bytes,
  });
  const file = await readJson(chunkRes, `upload chunk ${name}`);
  const itemId = file?.itemId;
  if (!itemId) {
    throw new Error(
      `[e2e] upload of ${name} did not finalize into a File with itemId: ` +
        `${JSON.stringify(file).slice(0, 300)}`
    );
  }
  // eslint-disable-next-line no-console
  console.log(`[e2e] uploaded ${name} -> item ${itemId} (${bytes.length} bytes)`);
  return { itemId, itemName: name };
}

// Delete a provisioned folder (teardown).
export async function deleteFolder(
  request: APIRequestContext,
  token: string,
  folderId: string
): Promise<boolean> {
  const res = await request.delete(apiUrl(`/folder/${folderId}`), {
    headers: { 'Girder-Token': token },
  });
  if (res.status() >= 300) {
    // eslint-disable-next-line no-console
    console.warn(`[e2e] delete folder ${folderId} returned HTTP ${res.status()}`);
    return false;
  } else {
    // eslint-disable-next-line no-console
    console.log(`[e2e] deleted folder ${folderId}`);
    return true;
  }
}
