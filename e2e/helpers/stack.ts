import { execFileSync } from 'child_process';
import { createHash } from 'crypto';
import { existsSync, readFileSync, readdirSync } from 'fs';
import * as path from 'path';
import { APIRequestContext } from '@playwright/test';
import { CONFIG, apiUrl } from './config';

// The suite assumes an already-deployed paired stack; it does not manage docker.
// The stack must satisfy four things:
//
//   1. girder reachable at CONFIG.baseURL with CONFIG's credentials;
//   2. the backend is THIS worktree's girder_volview;
//   3. the SPA at static/built/plugins/volview/ is the paired,
//      processing-enabled VolView build — a plain `girder build` re-clobbers it
//      with the pinned npm package, which drops the save button and breaks the
//      save/restore specs;
//   4. a deploy receipt (below) written next to the served index.html.
//
// Writing the receipt is the deploy tooling's LAST step, so its presence
// certifies the rest.

const VERSION_URL = apiUrl('/system/version');

const RECEIPT_HINT =
  'Deploy the paired stack, writing deployed-heads.json next to the served ' +
  'index.html as the last step (see the contract atop this file).';

const BRING_UP_HINT =
  `\n${RECEIPT_HINT}\n` +
  `Then wait for  curl -f ${VERSION_URL}  to succeed.`;

async function versionReachable(request: APIRequestContext): Promise<boolean> {
  try {
    const res = await request.get(VERSION_URL, { timeout: 10_000 });
    return res.ok();
  } catch {
    return false;
  }
}

// Single-shot health check so the tests never spin against a dead stack.
export async function healthCheck(request: APIRequestContext): Promise<void> {
  if (await versionReachable(request)) return;
  throw new Error(`[e2e] girder is not reachable at ${VERSION_URL}.${BRING_UP_HINT}`);
}

// Deploy guard. The deploy step writes a receipt recording the worktree HEADs it
// deployed. Refuse to run unless the stack serves THIS worktree's current HEAD,
// so a stale deploy fails loud instead of surfacing as a confusing mid-test
// assertion against a different checkout.
const RECEIPT_URL = `${CONFIG.baseURL}/static/built/plugins/volview/deployed-heads.json`;
const INDEX_URL = `${CONFIG.baseURL}/static/built/plugins/volview/index.html`;

export type DeployReceipt = {
  girderWorktree?: string;
  girderSha?: string;
  girderShort?: string;
  volviewWorktree?: string;
  volviewSha?: string;
  volviewShort?: string;
  indexMd5?: string;
  backendTreeMd5?: string;
};

// The receipt as served (compat setup records the deployed SHAs into its state).
export async function fetchDeployReceipt(request: APIRequestContext): Promise<DeployReceipt> {
  const res = await request.get(RECEIPT_URL, { timeout: 10_000 });
  if (!res.ok()) throw new Error(`[e2e] no deploy receipt at ${RECEIPT_URL} (HTTP ${res.status()})`);
  return JSON.parse(await res.text());
}

function gitHead(dir: string): string | null {
  try {
    return execFileSync('git', ['-C', dir, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  } catch {
    return null;
  }
}

function md5(data: Buffer | string): string {
  return createHash('md5').update(data).digest('hex');
}

function pythonFiles(root: string, dir = root): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      // The installed Python wheel excludes the web client's npm dependencies;
      // some packages contain Python source files, which must not affect the
      // backend deployment fingerprint.
      if (path.relative(root, fullPath) !== path.join('web_client', 'node_modules')) {
        files.push(...pythonFiles(root, fullPath));
      }
    }
    else if (entry.isFile() && entry.name.endsWith('.py')) files.push(fullPath);
  }
  return files;
}

// Mirrors script/deploy's `find | sort | xargs md5sum | md5sum` receipt hash.
function pythonTreeMd5(root: string): string {
  const digestLines = pythonFiles(root)
    .map((file) => `./${path.relative(root, file).split(path.sep).join('/')}`)
    .sort()
    .map(
      (relative) =>
        `${md5(readFileSync(path.join(root, relative.slice(2))))}  ${relative}\n`
    )
    .join('');
  return md5(digestLines);
}

export async function verifyDeployedHeads(request: APIRequestContext): Promise<void> {
  let receipt: DeployReceipt;
  try {
    const res = await request.get(RECEIPT_URL, { timeout: 10_000 });
    if (!res.ok()) throw new Error(`HTTP ${res.status()}`);
    receipt = JSON.parse(await res.text());
  } catch (e) {
    throw new Error(
      `[e2e] no deploy receipt at ${RECEIPT_URL} (${(e as Error).message}).\n` +
        `Without a receipt the stack is presumed to serve stock code — not this\n` +
        `worktree. ${RECEIPT_HINT}`
    );
  }

  // The harness lives at <girder_volview worktree>/e2e/helpers, so the worktree
  // root is two dirs up. Prove the stack serves THIS worktree's HEAD — unless
  // E2E_EXPECT_GIRDER_SHA overrides the expectation (the compat capture phase
  // runs these specs against a deliberately different deploy, e.g. main).
  const override = process.env.E2E_EXPECT_GIRDER_SHA;
  const worktreeRoot = path.resolve(__dirname, '..', '..');
  const expected = override || gitHead(worktreeRoot);
  if (override) {
    // eslint-disable-next-line no-console
    console.log(
      `[e2e] E2E_EXPECT_GIRDER_SHA set: expecting deployed girder ${override.slice(0, 9)}`
    );
  }
  // Both halves of the comparison must exist, or the guard is not a guard: a
  // missing sha on either side is when a wrong deploy is most likely, not least.
  if (!receipt.girderSha) {
    throw new Error(
      `[e2e] the deploy receipt at ${RECEIPT_URL} has no girderSha, so it cannot ` +
        `certify what the stack serves. ${RECEIPT_HINT}`
    );
  }
  if (!expected) {
    throw new Error(
      `[e2e] cannot determine the expected girder_volview sha: ${worktreeRoot} is not a\n` +
        `git checkout and E2E_EXPECT_GIRDER_SHA is unset. Set E2E_EXPECT_GIRDER_SHA to the\n` +
        `sha the stack should be serving (the compat harness does this for its baseline,\n` +
        `which is a git-archive export with no .git of its own).`
    );
  }
  if (expected !== receipt.girderSha) {
    throw new Error(
      `[e2e] deploy is stale: the stack serves girder_volview ${receipt.girderShort} ` +
        `but the expected sha is ${expected.slice(0, 9)}` +
        `${override ? ' (from E2E_EXPECT_GIRDER_SHA)' : ' (this worktree HEAD)'}.\n` +
        `Redeploy and refresh the receipt. ${RECEIPT_HINT}`
    );
  }

  const volviewOverride = process.env.E2E_EXPECT_VOLVIEW_SHA;
  const receiptVolviewHead = receipt.volviewWorktree
    ? gitHead(receipt.volviewWorktree)
    : null;
  const expectedVolview = volviewOverride || receiptVolviewHead;
  if (volviewOverride) {
    // eslint-disable-next-line no-console
    console.log(
      `[e2e] E2E_EXPECT_VOLVIEW_SHA set: expecting deployed VolView ${volviewOverride.slice(0, 9)}`
    );
  }
  if (!receipt.volviewSha) {
    throw new Error(
      `[e2e] the deploy receipt at ${RECEIPT_URL} has no volviewSha, so it cannot ` +
        `certify which client the stack serves. ${RECEIPT_HINT}`
    );
  }
  if (!expectedVolview) {
    throw new Error(
      `[e2e] cannot determine the expected VolView sha: the receipt's VolView worktree ` +
        `is unavailable and E2E_EXPECT_VOLVIEW_SHA is unset. Set E2E_EXPECT_VOLVIEW_SHA ` +
        `to the client sha the stack should serve.`
    );
  }
  if (expectedVolview !== receipt.volviewSha) {
    throw new Error(
      `[e2e] deploy is stale: the stack serves VolView ${receipt.volviewShort}, but the ` +
        `expected sha is ${expectedVolview.slice(0, 9)}` +
        `${volviewOverride ? ' (from E2E_EXPECT_VOLVIEW_SHA)' : ' (receipt worktree HEAD)'}.\n` +
        `Redeploy and refresh the receipt. ${RECEIPT_HINT}`
    );
  }

  if (!receipt.indexMd5) {
    throw new Error(`[e2e] deploy receipt has no indexMd5. ${RECEIPT_HINT}`);
  }
  const servedIndex = await request.get(INDEX_URL, { timeout: 10_000 });
  if (!servedIndex.ok()) {
    throw new Error(
      `[e2e] cannot read deployed VolView index at ${INDEX_URL} (HTTP ${servedIndex.status()})`
    );
  }
  const servedIndexMd5 = md5(await servedIndex.body());
  if (servedIndexMd5 !== receipt.indexMd5) {
    throw new Error(
      `[e2e] deployed VolView index hash is ${servedIndexMd5}, but the receipt records ` +
        `${receipt.indexMd5}. Redeploy and refresh the receipt.`
    );
  }

  if (receipt.volviewWorktree && existsSync(receipt.volviewWorktree)) {
    const builtIndex = path.join(receipt.volviewWorktree, 'dist', 'index.html');
    if (!existsSync(builtIndex)) {
      throw new Error(
        `[e2e] receipt VolView worktree has no built index: ${builtIndex}`
      );
    }
    const builtIndexMd5 = md5(readFileSync(builtIndex));
    if (builtIndexMd5 !== receipt.indexMd5) {
      throw new Error(
        `[e2e] VolView worktree build hash is ${builtIndexMd5}, but the deployed receipt ` +
          `records ${receipt.indexMd5}. Redeploy before running the browser suite.`
      );
    }
  }

  if (!receipt.backendTreeMd5) {
    throw new Error(`[e2e] deploy receipt has no backendTreeMd5. ${RECEIPT_HINT}`);
  }
  // The receipt's backendTreeMd5 covers the installed PACKAGE directory, so hash
  // the worktree's package directory too -- hashing the worktree root sweeps in
  // setup.py, tests/ and e2e/seed/, which can never match.
  const backendPkg = path.join(receipt.girderWorktree ?? '', 'girder_volview');
  if (receipt.girderWorktree && existsSync(backendPkg)) {
    const currentBackendMd5 = pythonTreeMd5(backendPkg);
    if (currentBackendMd5 !== receipt.backendTreeMd5) {
      throw new Error(
        `[e2e] girder_volview worktree hash is ${currentBackendMd5}, but the deployed ` +
          `receipt records ${receipt.backendTreeMd5}. Redeploy before running the browser suite.`
      );
    }
  }

  // eslint-disable-next-line no-console
  console.log(
    `[e2e] deploy receipt OK — girder ${receipt.girderShort}, VolView ${receipt.volviewShort}.`
  );
}
