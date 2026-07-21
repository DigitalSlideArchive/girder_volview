import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { APIRequestContext } from '@playwright/test';
import { CONFIG, apiUrl } from './config';
import { readJson } from './http';
import { makeNrrd } from './nrrd';
import { authenticate, createFolderUnder, uploadFile, deleteFolder } from './provision';
import { CompatState, FixtureFolder, FixtureId } from './compat-state';

// Provision one run root containing an isolated folder for every scenario.
// NRRD fixtures are generated in memory; grouped fixtures plain-upload cached
// IDC DICOM slices and an item-list config that groups on meta.dicom.*.

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const SEED_CLI = path.resolve(__dirname, '..', 'seed', 'seed.py');
const DICOM_LI_CONFIG = path.resolve(__dirname, '..', 'fixtures', 'dicom.large_image_config.yaml');

function seedSmallDicom(folderId: string): void {
  // eslint-disable-next-line no-console
  console.log(`[compat] seeding small DICOM tier into folder ${folderId} (uv run seed.py)`);
  execFileSync('uv', ['run', SEED_CLI, 'seed-small', '--folder-id', folderId, '--slices', '12'], {
    cwd: REPO_ROOT,
    stdio: 'inherit',
    env: {
      ...process.env,
      GIRDER_URL: CONFIG.baseURL,
      DSA_ADMIN_USER: CONFIG.user,
      DSA_ADMIN_PASS: CONFIG.pass,
    },
  });
}

async function listItems(
  request: APIRequestContext,
  token: string,
  folderId: string
): Promise<Array<{ _id: string; name: string }>> {
  const res = await request.get(apiUrl(`/item?folderId=${folderId}&limit=1000`), {
    headers: { 'Girder-Token': token },
  });
  return readJson(res, `list items of ${folderId}`);
}

// Probe for the optional devkit tier: the trial folder of the "VolView Devkit"
// collection, when the full devkit has been seeded.
export async function findDevkitTrialFolder(
  request: APIRequestContext,
  token: string
): Promise<string | undefined> {
  const collRes = await request.get(
    apiUrl(`/collection?text=${encodeURIComponent('VolView Devkit')}&limit=10`),
    { headers: { 'Girder-Token': token } }
  );
  const collections: Array<{ _id: string; name: string }> = await collRes.json();
  const devkit = collections.find?.((c) => c.name === 'VolView Devkit');
  if (!devkit) return undefined;
  const folderRes = await request.get(
    apiUrl(`/folder?parentType=collection&parentId=${devkit._id}&name=trial`),
    { headers: { 'Girder-Token': token } }
  );
  const folders: Array<{ _id: string }> = await folderRes.json();
  return folders?.[0]?._id;
}

export async function provisionCompat(
  request: APIRequestContext,
  deployed: { sourceGirderSha: string; sourceVolviewSha: string }
): Promise<CompatState> {
  const { token, userId } = await authenticate(request);

  const runId = `${Date.now()}-${Math.floor(Math.random() * 1e4)}`;
  const runRootFolderId = await createFolderUnder(
    request,
    token,
    'user',
    userId,
    `girder-volview-compat-${runId}`
  );
  const fixtures = {} as Record<FixtureId, FixtureFolder>;

  async function provision(): Promise<CompatState> {
  async function nrrdFixture(id: FixtureId, count: 1 | 2): Promise<void> {
    const folderId = await createFolderUnder(request, token, 'folder', runRootFolderId, id);
    const uploaded = [];
    for (let index = 0; index < count; index += 1) {
      uploaded.push(
        await uploadFile(
          request,
          token,
          folderId,
          `synthetic-${index + 1}.nrrd`,
          makeNrrd({ variant: index })
        )
      );
    }
    fixtures[id] = {
      folderId,
      itemIds: uploaded.map((item) => item.itemId),
      itemNames: uploaded.map((item) => item.itemName),
    };
  }

  async function dicomFixture(id: FixtureId): Promise<void> {
    const folderId = await createFolderUnder(request, token, 'folder', runRootFolderId, id);
    // large_image's grouped recursive endpoint assumes a flattened folder has
    // at least one descendant. Keep the config and resulting session items at
    // the scenario root, and put the source slices in a public child folder.
    // This also matches the hierarchy shape that flatten/group is meant for.
    const dataFolderId = await createFolderUnder(request, token, 'folder', folderId, 'dicom');
    seedSmallDicom(dataFolderId);
    await uploadFile(
      request,
      token,
      folderId,
      '.large_image_config.yaml',
      fs.readFileSync(DICOM_LI_CONFIG)
    );
    const dicomItems = await listItems(request, token, dataFolderId);
    const images = dicomItems.filter((item) => item.name.endsWith('.dcm'));
    if (images.length === 0) throw new Error(`[compat] fixture '${id}' contains no DICOM items`);
    fixtures[id] = {
      folderId,
      itemIds: images.map((item) => item._id),
      itemNames: images.map((item) => item.name),
    };
  }

  await nrrdFixture('single-item', 1);
  await nrrdFixture('checked-nrrd', 2);
  await dicomFixture('filtered-dicom');
  await dicomFixture('study-layered');

  await nrrdFixture('lifecycle-single', 1);
  await nrrdFixture('lifecycle-checked', 2);
  await dicomFixture('lifecycle-filter');
  await nrrdFixture('lifecycle-restart', 2);
  await nrrdFixture('lifecycle-bare', 2);
  await nrrdFixture('lifecycle-older', 2);
  await nrrdFixture('lifecycle-session-row', 2);
  await nrrdFixture('lifecycle-url-contract', 2);
  await nrrdFixture('jobs-comeback', 1);
  await nrrdFixture('jobs-live', 1);
  await nrrdFixture('jobs-staged', 1);
  await nrrdFixture('jobs-failure', 1);

  const devkitTrialFolderId = await findDevkitTrialFolder(request, token);

  return {
    createdAt: new Date().toISOString(),
    sourceGirderSha: deployed.sourceGirderSha,
    sourceVolviewSha: deployed.sourceVolviewSha,
    runRootFolderId,
    fixtures,
    token,
    provisioned: true,
    dicomSeeded: true,
    devkitTrialFolderId,
    gestures: [],
  };
  }

  try {
    return await provision();
  } catch (error) {
    if (!(await deleteFolder(request, token, runRootFolderId))) {
      const message = error instanceof Error ? error.message : String(error);
      throw new Error(
        `[compat] provisioning failed and could not delete run root ${runRootFolderId}: ${message}`
      );
    }
    throw error;
  }
}

export async function teardownCompat(request: APIRequestContext, state: CompatState): Promise<void> {
  let token = state.token;
  try {
    token = (await authenticate(request)).token;
  } catch {
    /* use the stored token */
  }
  await deleteFolder(request, token, state.runRootFolderId);
}
