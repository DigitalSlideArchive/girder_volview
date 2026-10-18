import { test as base, expect, APIRequestContext, Page } from '@playwright/test';
import { apiUrl } from '../helpers/config';
import { requireHarnessState, plantCookie } from '../helpers/girder';
import { gotoFolder, loginViaUI, openInVolView } from '../helpers/girder-ui';
import { resourceNames } from '../helpers/manifest';
import { makeNrrd } from '../helpers/nrrd';
import { openModuleTab } from '../helpers/volview';
import {
  authenticate,
  createFolderUnder,
  deleteFolder,
  uploadFile,
} from '../helpers/provision';

type BrowseFolder = { folderId: string; token: string };

const test = base.extend<{ browseFolder: BrowseFolder }>({
  browseFolder: async ({ request, context }, use) => {
    const { token } = await authenticate(request);
    const folderId = await createFolderUnder(
      request,
      token,
      'folder',
      requireHarnessState().runRootFolderId,
      'folder-browsing'
    );
    await plantCookie(context, token);
    try {
      await use({ folderId, token });
    } finally {
      expect(await deleteFolder(request, token, folderId), 'fixture folder cleanup failed').toBe(true);
    }
  },
});

async function browse(
  page: Page,
  folderId: string,
  loadable: boolean,
  navigate: () => Promise<unknown>
): Promise<void> {
  const eligibilityPath = new URL(apiUrl(`/folder/${folderId}/volview_loadable`)).pathname;
  const eligibility = page.waitForRequest((request) =>
    request.method() === 'GET' && new URL(request.url()).pathname === eligibilityPath
  );
  await navigate();
  const response = await (await eligibility).response();
  if (!response) throw new Error('folder eligibility request was aborted');
  expect(response.ok(), 'folder eligibility request failed').toBe(true);
  expect(await response.json()).toEqual({ loadable });
  await expect(page).toHaveURL(new RegExp(`(?:#|/)folder/${folderId}$`));
  const button = page.getByRole('button', { name: 'Open Folder in VolView' });
  if (loadable) await expect(button).toBeVisible();
  else await expect(page.locator('.open-in-volview')).toBeHidden();
}

async function expectImageLaunch(page: Page, name: string): Promise<void> {
  const launch = await openInVolView(page);
  try {
    expect(resourceNames(await launch.manifest).filter((resource) => resource !== 'config.json'))
      .toEqual([name]);
    const canvas = launch.popup.locator('[data-testid~="vtk-two-view"] canvas').first();
    await expect(canvas).toBeVisible();
    await expect.poll(() => canvas.evaluate((element: HTMLCanvasElement) =>
      element.width > 0 && element.height > 0
    )).toBe(true);
    await openModuleTab(launch.popup, 'Data');
    await expect(launch.popup.getByText(name, { exact: true }).first()).toBeVisible();
    await expect(launch.popup.getByText('Dims: (16, 16, 16)', { exact: true })).toBeVisible();
  } finally {
    await launch.popup.close();
  }
}

async function setTransient(
  request: APIRequestContext, token: string, itemId: string, transient: boolean
): Promise<void> {
  const response = await request.put(apiUrl(`/item/${itemId}/metadata`), {
    headers: { 'Girder-Token': token },
    data: { volviewTransient: transient },
  });
  expect(response.ok(), 'updating staged-input metadata failed').toBe(true);
}

test('browses to an unsupported leaf, then opens a newly uploaded image from its ancestor', async ({
  page, request, browseFolder,
}) => {
  const { folderId, token } = browseFolder;
  const studyId = await createFolderUnder(request, token, 'folder', folderId, 'study');
  const seriesId = await createFolderUnder(request, token, 'folder', studyId, 'series');
  await uploadFile(request, token, seriesId, 'readme.txt', Buffer.from('Study notes'));

  await loginViaUI(page);
  await browse(page, folderId, false, () => gotoFolder(page, folderId));
  await browse(page, studyId, false, () =>
    page.locator(`a.g-folder-list-link[href="#folder/${studyId}"]`).click()
  );
  await browse(page, seriesId, false, () =>
    page.locator(`a.g-folder-list-link[href="#folder/${seriesId}"]`).click()
  );
  await expect(page.getByRole('link', { name: 'readme.txt' })).toBeVisible();

  await uploadFile(request, token, seriesId, 'image.nrrd', makeNrrd());
  await browse(page, seriesId, true, () => page.reload({ waitUntil: 'domcontentloaded' }));
  await expect(page.getByRole('link', { name: 'image.nrrd' })).toBeVisible();
  await browse(page, folderId, true, () =>
    page.locator('.g-hierarchy-breadcrumb-bar').getByText('folder-browsing', { exact: true }).click()
  );
  await expectImageLaunch(page, 'image.nrrd');
});

test('excludes staged leaf images across a parent batch and reflects metadata changes on revisit', async ({
  page, request, browseFolder,
}) => {
  const { folderId, token } = browseFolder;
  const seriesId = await createFolderUnder(request, token, 'folder', folderId, 'series');
  const bytes = makeNrrd();
  let restoredItemId = '';
  let restoredName = '';
  for (let index = 0; index < 129; index += 1) {
    const name = `staged-${index + 1}.nrrd`;
    const { itemId } = await uploadFile(request, token, seriesId, name, bytes);
    await setTransient(request, token, itemId, true);
    restoredItemId = itemId;
    restoredName = name;
  }

  await loginViaUI(page);
  await browse(page, folderId, false, () => gotoFolder(page, folderId));
  await browse(page, seriesId, false, () =>
    page.locator(`a.g-folder-list-link[href="#folder/${seriesId}"]`).click()
  );
  await expect(page.getByRole('link', { name: 'staged-1.nrrd' })).toBeVisible();

  await setTransient(request, token, restoredItemId, false);
  await browse(page, folderId, true, () =>
    page.locator('.g-hierarchy-breadcrumb-bar').getByText('folder-browsing', { exact: true }).click()
  );
  await expectImageLaunch(page, restoredName);

  await setTransient(request, token, restoredItemId, true);
  await browse(page, folderId, false, () => page.reload({ waitUntil: 'domcontentloaded' }));
});
