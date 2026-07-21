import { Page, TestInfo, expect } from '@playwright/test';

// VolView is ready once a vtk view canvas has real (non-zero) dimensions — the
// same signal the VolView wdio suite uses (waitForViews).
export async function waitForVolViewReady(page: Page, timeout = 90_000) {
  await page.locator('[data-testid~="vtk-view"] canvas').first().waitFor({ state: 'attached', timeout });
  await page.waitForFunction(
    () => {
      const canvases = document.querySelectorAll('[data-testid~="vtk-view"] canvas');
      return Array.from(canvases).some(
        (c) => (c as HTMLCanvasElement).width > 10 && (c as HTMLCanvasElement).height > 10
      );
    },
    undefined,
    { timeout }
  );
  // Let the first frame settle for a faithful screenshot.
  await page.waitForTimeout(1500);
}

// The current value of the tab's `urls=` launch param (decoded). This is where
// resume-vs-fresh lives: F5 re-fetches exactly this.
export function urlsParam(page: Page): string {
  const u = new URL(page.url());
  return u.searchParams.get('urls') || '';
}

// Screenshot into the test's output dir with a stable, human-scannable name.
export async function shot(page: Page, info: TestInfo, name: string) {
  const file = info.outputPath(`${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  await info.attach(name, { path: file, contentType: 'image/png' });
}

// Trigger the girder-launched REMOTE save (ControlsStrip save button ->
// remote-save-state.saveState() -> POST save= -> resumeUrl repoint). Returns the
// resumeUrl the backend handed back. No dialog is shown when save= is set.
export async function remoteSave(page: Page): Promise<string> {
  const saveButton = page.locator('button:has(i.mdi-content-save-all)').first();
  await expect(saveButton, 'save button (mdi-content-save-all) not found').toBeVisible();

  const savePost = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' &&
      /\/(item|folder)\/[^/]+\/volview(\?|$)/.test(r.url()),
    { timeout: 60_000 }
  );
  await saveButton.click();
  // Main-era clients interpose a "Saving Session State" filename dialog even
  // for remote saves; confirm it. The branch client saves directly, so the
  // button simply never appears.
  const confirmSave = page.locator('[data-testid="save-session-confirm-button"]').first();
  if (await confirmSave.isVisible({ timeout: 3_000 }).catch(() => false)) {
    await confirmSave.click();
  }
  const res = await savePost;
  expect(res.status(), `save POST failed: ${res.status()} ${res.url()}`).toBeLessThan(300);

  let resumeUrl = '';
  try {
    const body = await res.json();
    resumeUrl = body?.resumeUrl || '';
  } catch {
    /* fail-safe: some responses may not be JSON */
  }
  // The client repoints urls= via history.replaceState after a resumeUrl.
  if (resumeUrl) {
    await expect
      .poll(() => urlsParam(page), { timeout: 15_000, message: 'urls= did not repoint to resumeUrl' })
      .toBe(resumeUrl);
  }
  // Best-effort: the "Save Successful" toast.
  await page
    .getByText('Save Successful', { exact: false })
    .waitFor({ state: 'visible', timeout: 10_000 })
    .catch(() => undefined);
  return resumeUrl;
}

// Click a module tab by name (Jobs / Annotations / Rendering / Data). The Jobs
// tab appears only when the launch config= registered a processing provider;
// its job list is folder+user scoped.
export async function openModuleTab(page: Page, name: string): Promise<void> {
  await page.locator(`button[data-testid="module-tab-${name}"]`).click();
}

// Open the Jobs tab and click the first succeeded job's "Load" — the come-back
// path: this fetches the results AND applies them through the same
// intent-honoring pipeline the live flow uses (labelmap → segment group on the
// reconstructed parent image, plain image → new dataset). The button is
// consumed once the scene application finishes.
export async function loadJobResults(page: Page): Promise<void> {
  await openModuleTab(page, 'Jobs');
  const panel = page.locator('.jobs-module');
  const load = panel.getByRole('button', { name: 'Load', exact: true }).first();
  await expect(load, 'no "Load" button — is the succeeded job listed in the Jobs tab?').toBeVisible();
  await load.click();
  await expect(load, 'the job result did not finish applying').toHaveCount(0);
}

// Select a registered task in the Jobs tab's TaskPicker (a v-select labelled
// "Task"), matching by title prefix (e.g. "Otsu"). Vuetify's floating label is
// not programmatically associated with the input, so match the v-select itself.
export async function selectTask(page: Page, titlePrefix: string): Promise<void> {
  await openModuleTab(page, 'Jobs');
  const picker = page.locator('.jobs-module .v-select', { hasText: 'Task' }).first();
  await expect(picker, 'no Task picker — is a processing provider registered?').toBeVisible();
  await picker.click();
  const option = page
    .locator('.v-overlay-container .v-list-item, .v-menu .v-list-item')
    .filter({ hasText: titlePrefix })
    .first();
  await expect(option, `no "${titlePrefix}*" task option in the picker`).toBeVisible();
  await option.click();
}

// Wait for the auto-bound image input to render (FileWidget's "Active dataset"
// caption under the bound image name) before submit — a volume with no server
// provenance blocks submit, so this proves the binding step ran.
export async function waitForInputBound(page: Page, timeout = 30_000): Promise<void> {
  await expect(
    page.locator('.jobs-module').getByText('Active dataset').first(),
    'the image input never bound to the active dataset'
  ).toBeVisible({ timeout });
}

export async function submitTaskFromForm(page: Page): Promise<void> {
  const submit = page
    .locator('.jobs-module')
    .getByRole('button', { name: 'Submit', exact: true });
  await expect(submit, 'Submit button not enabled (unbound input / form invalid?)').toBeEnabled();
  await submit.click();
}

// Wait for the live completion toast ("Job complete: <task>") the store raises
// once a job submitted THIS session reaches success — the live path, not a
// re-discovered history row.
export async function waitForJobComplete(page: Page, timeout = 180_000): Promise<void> {
  await expect(
    page.locator('.Vue-Toastification__toast', { hasText: 'Job complete' }).first(),
    'no "Job complete" toast — did the live job finish?'
  ).toBeVisible({ timeout });
}
