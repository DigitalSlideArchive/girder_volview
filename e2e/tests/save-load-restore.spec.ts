import { test, expect, Page } from '@playwright/test';
import {
  gotoFolder,
  loginViaUI,
  checkRowByItemId,
  checkRowByTexts,
  fillFilterBox,
  uncheckAllRows,
  openInVolView,
  openFromItemPage,
} from '../helpers/girder-ui';
import {
  setupFixture,
  countSessionItems,
  firstFileId,
  fetchManifest,
  resourceUrls,
  CONFIG,
  Girder,
} from '../helpers/girder';
import { FixtureId } from '../helpers/compat-state';
import {
  waitForVolViewReady,
  urlsParam,
  remoteSave,
  shot,
} from '../helpers/volview';
import { placeRuler, readRulerMeasurements } from '../helpers/annotations';
import {
  isSessionManifest,
  resourceNames,
  reloadCapturingManifest,
} from '../helpers/manifest';

// Current-to-current lifecycle coverage. Every scenario owns a provisioned
// folder, and every launch goes through the deployed Girder UI and open.js.

const PATIENT2 = 'ACRIN-NSCLC-FDG-PET-022';

type Gesture = 'single-item' | 'checked' | 'filter' | 'bare-folder';
type Launched = { view: Page; freshManifest: string; manifest: any };

async function launchGesture(driver: Page, g: Girder, gesture: Gesture): Promise<Launched> {
  let launch;
  if (gesture === 'single-item') {
    launch = await openFromItemPage(driver, g.itemId);
  } else {
    await gotoFolder(driver, g.folderId);
    if (gesture === 'checked') {
      await checkRowByItemId(driver, g.itemId);
    } else if (gesture === 'filter') {
      await fillFilterBox(driver, PATIENT2);
      await checkRowByTexts(driver, [PATIENT2]);
    } else {
      await uncheckAllRows(driver);
    }
    launch = await openInVolView(driver);
  }
  await waitForVolViewReady(launch.popup);
  return {
    view: launch.popup,
    freshManifest: urlsParam(launch.popup),
    manifest: await launch.manifest,
  };
}

function expectFreshRoute(g: Girder, gesture: Gesture, manifestUrl: string): void {
  const route = new URL(manifestUrl, CONFIG.baseURL);
  if (gesture === 'single-item') {
    expect(route.pathname).toBe(`/${CONFIG.apiRoot}/item/${g.itemId}/volview`);
    return;
  }
  expect(route.pathname).toBe(`/${CONFIG.apiRoot}/folder/${g.folderId}/volview`);
  if (gesture === 'checked') {
    expect(route.searchParams.get('items')).toBe(g.itemId);
    expect(route.searchParams.has('folders')).toBeTruthy();
  } else if (gesture === 'filter') {
    expect(route.searchParams.has('filters'), 'grouped launch emitted no filters= leg').toBeTruthy();
    expect(JSON.parse(route.searchParams.get('filters') || '[]')).not.toEqual([]);
  } else {
    expect(route.searchParams.has('items')).toBeTruthy();
    expect(route.searchParams.get('items')).toBe('');
    expect(route.searchParams.has('folders')).toBeTruthy();
    expect(route.searchParams.get('folders')).toBe('');
  }
}

test.describe('save/load/restore F5 lifecycle', () => {
  const cases: Array<{ gesture: Exclude<Gesture, 'bare-folder'>; fixture: FixtureId }> = [
    { gesture: 'single-item', fixture: 'lifecycle-single' },
    { gesture: 'checked', fixture: 'lifecycle-checked' },
    { gesture: 'filter', fixture: 'lifecycle-filter' },
  ];

  for (const { gesture, fixture } of cases) {
    test(`${gesture}: fresh -> F5-stays-fresh -> save -> F5-resumes -> save-again -> F5-resumes`, async ({
      page,
      context,
    }, info) => {
      const g = await setupFixture(context, fixture);
      const launched = await launchGesture(page, g, gesture);
      const view = launched.view;
      const freshManifest = launched.freshManifest;
      const m1 = launched.manifest;
      await shot(view, info, `${gesture}-1-launch-fresh`);
      expectFreshRoute(g, gesture, freshManifest);
      expect(
        isSessionManifest(m1),
        `fresh launch must not load a session zip: ${resourceNames(m1)}`
      ).toBeFalsy();
      expect(resourceNames(m1).some((name) => name !== 'config.json')).toBeTruthy();

      const m2 = await reloadCapturingManifest(view);
      await shot(view, info, `${gesture}-2-f5-stays-fresh`);
      expect(urlsParam(view), 'F5-before-save must not repoint').toBe(freshManifest);
      expect(isSessionManifest(m2), 'F5-before-save pulled in a session').toBeFalsy();

      await placeRuler(view);
      const savedRulers = await readRulerMeasurements(view);
      expect(savedRulers).toHaveLength(1);

      const sessionsBefore = await countSessionItems(page.request, g);
      const resumeUrl1 = await remoteSave(view);
      await shot(view, info, `${gesture}-3-after-save`);
      expect(resumeUrl1, 'save response carried no resumeUrl').toBeTruthy();
      expect(urlsParam(view), 'urls= must repoint to the save resumeUrl').toBe(resumeUrl1);
      if (gesture !== 'single-item') {
        expect(await countSessionItems(page.request, g)).toBeGreaterThan(sessionsBefore);
      }

      const m4 = await reloadCapturingManifest(view);
      await shot(view, info, `${gesture}-4-f5-resumes-save`);
      expect(urlsParam(view), 'F5-after-save must stay on the resumeUrl').toBe(resumeUrl1);
      expect(isSessionManifest(m4), 'F5-after-save did not load the session').toBeTruthy();
      expect(await readRulerMeasurements(view), 'F5-after-save lost the saved ruler').toEqual(
        savedRulers
      );

      if (gesture === 'filter' || gesture === 'checked') {
        const reopenDriver = await context.newPage();
        const reopened = await launchGesture(reopenDriver, g, gesture);
        const shouldResume = gesture === 'filter';
        expect(
          isSessionManifest(reopened.manifest),
          shouldResume
            ? `reopening filter did not resume its save: ${resourceNames(reopened.manifest)}`
            : `reopening checked raw images did not start fresh: ${resourceNames(reopened.manifest)}`
        ).toBe(shouldResume);
        await reopened.view.close();
        await reopenDriver.close();
      }

      const resumeUrl2 = await remoteSave(view);
      await shot(view, info, `${gesture}-5-after-second-save`);
      expect(resumeUrl2, 'second save carried no resumeUrl').toBeTruthy();
      expect(urlsParam(view)).toBe(resumeUrl2);
      await reloadCapturingManifest(view);
      await shot(view, info, `${gesture}-6-f5-resumes-second-save`);
      expect(urlsParam(view), 'F5 after the second save left its resumeUrl').toBe(resumeUrl2);
    });
  }

  test('fresh restart via checked raw images starts clean, then F5 resumes the new save', async ({
    page,
    context,
  }, info) => {
    const g = await setupFixture(context, 'lifecycle-restart');
    const seed = await launchGesture(page, g, 'checked');
    const olderResume = await remoteSave(seed.view);
    expect(olderResume, 'seeding save carried no resumeUrl').toBeTruthy();
    await seed.view.close();

    const restart = await launchGesture(page, g, 'checked');
    await shot(restart.view, info, 'restart-1-fresh-despite-older-save');
    expectFreshRoute(g, 'checked', restart.freshManifest);
    expect(
      isSessionManifest(restart.manifest),
      `restart resumed the older save: ${resourceNames(restart.manifest)}`
    ).toBeFalsy();

    const newResume = await remoteSave(restart.view);
    await shot(restart.view, info, 'restart-2-after-save');
    expect(newResume, 'restart save carried no resumeUrl').toBeTruthy();
    expect(newResume, 'the new save reused the older session item').not.toBe(olderResume);

    const manifest = await reloadCapturingManifest(restart.view);
    await shot(restart.view, info, 'restart-3-f5-resumes-new-save');
    expect(urlsParam(restart.view)).toBe(newResume);
    expect(isSessionManifest(manifest)).toBeTruthy();
  });

  test('bare folder-open resumes the newest folder-scoped save', async ({ page, context }, info) => {
    const g = await setupFixture(context, 'lifecycle-bare');
    const seed = await launchGesture(page, g, 'checked');
    const seededResume = await remoteSave(seed.view);
    expect(seededResume).toBeTruthy();
    await seed.view.close();

    const bare = await launchGesture(page, g, 'bare-folder');
    await shot(bare.view, info, 'bare-folder-resumes-newest');
    expectFreshRoute(g, 'bare-folder', bare.freshManifest);
    expect(
      isSessionManifest(bare.manifest),
      `bare open did not resume a session: ${resourceNames(bare.manifest)}`
    ).toBeTruthy();
  });

  test('checking an older session opens that save, not the newest', async ({ page, context }, info) => {
    const g = await setupFixture(context, 'lifecycle-older');
    const checked = await launchGesture(page, g, 'checked');
    await placeRuler(checked.view);
    const olderRulers = await readRulerMeasurements(checked.view);
    const olderResume = await remoteSave(checked.view);
    const newerResume = await remoteSave(checked.view);
    expect(olderResume).toBeTruthy();
    expect(newerResume).toBeTruthy();
    expect(newerResume).not.toBe(olderResume);
    await checked.view.close();

    const idOf = (resumeUrl: string) => resumeUrl.split('/item/')[1].split('/volview')[0];
    const olderId = idOf(olderResume);
    const newerId = idOf(newerResume);
    const olderFileId = await firstFileId(page.request, g.token, olderId);
    const newerFileId = await firstFileId(page.request, g.token, newerId);
    expect(olderFileId).not.toBe(newerFileId);

    // Session items are private even though the fixture folder and raw images
    // are public. Authenticate the Girder client before browsing those rows.
    await loginViaUI(page);
    await gotoFolder(page, g.folderId);
    await checkRowByItemId(page, olderId);
    const launch = await openInVolView(page);
    await waitForVolViewReady(launch.popup);
    await shot(launch.popup, info, 'older-session-reopened');

    const urls = urlsParam(launch.popup);
    expect(urls).toContain(`items=${olderId}`);
    expect(urls).not.toContain(`items=${newerId}`);
    const manifest = await fetchManifest(page.request, g.token, urls);
    const manifestUrls = resourceUrls(manifest).join(' ');
    expect(manifestUrls, 'the older save was not loaded').toContain(`/file/${olderFileId}/`);
    expect(manifestUrls, 'the newest save was substituted').not.toContain(`/file/${newerFileId}/`);
    expect(await readRulerMeasurements(launch.popup), 'the selected older session lost its ruler').toEqual(
      olderRulers
    );
  });

  test('the checked-image button carries the complete launch contract', async ({ page, context }) => {
    const g = await setupFixture(context, 'lifecycle-url-contract');
    await gotoFolder(page, g.folderId);
    await checkRowByItemId(page, g.itemId);

    const button = page.locator('.open-in-volview');
    await expect(button).toHaveText(/Open Checked in VolView/);
    const href = await button.getAttribute('href');
    expect(href, 'the open button carries no href').toBeTruthy();

    const actual = new URL(href!, CONFIG.baseURL);
    expect(actual.pathname).toBe('/static/built/plugins/volview/index.html');
    expect(actual.searchParams.get('names')).toBe('[manifest.json]');
    expect(actual.searchParams.get('config')).toBe(
      `/${CONFIG.apiRoot}/folder/${g.folderId}/volview_config/.volview_config.yaml`
    );

    const manifest = new URL(actual.searchParams.get('urls')!, CONFIG.baseURL);
    expect(manifest.pathname).toBe(`/${CONFIG.apiRoot}/folder/${g.folderId}/volview`);
    expect(manifest.searchParams.has('folders')).toBeTruthy();
    expect(manifest.searchParams.get('folders')).toBe('');
    expect(manifest.searchParams.get('items')).toBe(g.itemId);

    const save = new URL(actual.searchParams.get('save')!, CONFIG.baseURL);
    expect(save.pathname).toBe(`/${CONFIG.apiRoot}/folder/${g.folderId}/volview`);
    const linked = JSON.parse(save.searchParams.get('metadata') || '{}').linkedResources || {};
    expect(linked.items).toEqual([g.itemId]);
    expect(linked.folders || []).toEqual([]);
  });

  test('checking a saved session in Girder opens that session', async ({ page, context }) => {
    const g = await setupFixture(context, 'lifecycle-session-row');
    const checked = await launchGesture(page, g, 'checked');
    const resumeUrl = await remoteSave(checked.view);
    expect(resumeUrl).toBeTruthy();
    const sessionId = resumeUrl.split('/item/')[1].split('/volview')[0];
    await checked.view.close();

    await loginViaUI(page);
    await gotoFolder(page, g.folderId);
    await checkRowByItemId(page, sessionId);
    await checkRowByItemId(page, g.itemId);

    const popupPromise = page.waitForEvent('popup');
    await page.locator('.open-in-volview').click();
    await expect(page.locator('.modal-content')).toContainText('Will open newest VolView session');
    await page.locator('#g-confirm-button').click();
    const popup = await popupPromise;
    await popup.waitForLoadState('domcontentloaded');
    await waitForVolViewReady(popup);
    expect(urlsParam(popup)).toContain(`items=${sessionId}`);
  });
});
