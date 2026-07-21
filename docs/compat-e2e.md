# Browser lifecycle and backwards-compatibility e2e

This is the project's single browser-test infrastructure. It proves that
`session.volview.zip` files saved by an **older** girder_volview + VolView client
still restore and re-save correctly, then exercises fresh current-version
save/load/restore and job behavior.

```
e2e/scripts/compat.sh
 ├─ materialize-baseline.sh                          # git archive <pinned sha> -> e2e/.compat/
 ├─ script/deploy <baseline export> main             # baseline backend + client
 ├─ playwright --project capture                     # save sessions on the baseline
 ├─ script/deploy <this worktree> just-jobs          # branch backend + client
 ├─ playwright --project verify                      # old sessions must restore
 └─ playwright --project current                     # fresh lifecycle + jobs
```

Neither the old sources nor the session zips are committed — both are
reproducible from a sha. The repo stores a pointer (`e2e/compat-baseline.json`)
and the harness recreates the rest into the gitignored `e2e/.compat/`.

Girder's mongo volume survives the redeploy (script/deploy only recreates the
girder container's code), so the folders and sessions captured in step 2 are
still there for step 4. `e2e/.compat-state.json` (gitignored) bridges the two
playwright invocations: session item ids, launch descriptors, and the expected
content per gesture.

## What the harness proves

| Gesture                   | Launch (real girder UI, on the baseline)                    | Content saved                 | Verified on the branch                                                                                         |
| ------------------------- | ----------------------------------------------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `single-item`             | item page → Open in VolView                                 | ruler                         | item manifest serves the session; measurements exact; re-save                                                  |
| `checked-nrrd`            | check 2 NRRD rows → Open Checked                            | ruler + painted segment group | bare-folder open resumes it; checking the session row opens exactly it; groups + measurements survive; re-save |
| `filtered-dicom`          | filter box narrows to one patient → check series row → Open | ruler                         | replaying the same filter gesture resumes the matching `session.<filter>.volview.zip`                          |
| `study-layered`           | check CT + PET series rows of one study → Open              | PET layered over CT + ruler   | replay resumes; the layer survives restore and re-save                                                         |
| `devkit-study` (optional) | patient → study drill-down in the devkit collection         | PET layer + ruler             | replay resumes; skipped unless `seed.py seed` has run                                                          |

Content checks are semantic, not pixel-based: ruler measurement text must match
exactly, segment-group names must survive, and the re-saved manifest must keep
rulers/segment groups/layers (schema migrations are fine; content loss is not).
Screenshots are attached to the report as evidence, never asserted on.

The current project also covers what an old-session restore can't prove:

- fresh single-item, checked-image, and grouped-filter launches;
- F5 before save staying fresh, and F5 after first and second saves resuming;
- checked raw images deliberately restarting instead of resuming;
- bare-folder newest-session selection and exact older-session selection;
- the launch button's `urls`, `save`, `config`, and `names` contract;
- saved-session rows, completed-job loading, and live job auto-apply.

Each scenario owns a folder, so state can't leak between tests. Tests run on
one worker for predictable load, but aren't Playwright `serial` — one failure
doesn't mark the rest as unrun.

The DICOM fixtures use real IDC data (ACRIN NSCLC FDG-PET/CT, CC-BY): the
devkit's pinned `patient-01/study-01/{CT,PET}` + `patient-02/study-01/CT`
series at 12 slices each. Each grouped scenario gets its own uploaded copy,
with `meta.dicom.*` populated by girder_volview, plus
`e2e/fixtures/dicom.large_image_config.yaml` so the folder groups into
filterable series rows.

## Running it

One-time setup:

```bash
cd e2e && npm ci && npm run install-browser
uv run seed/seed.py fetch --small     # small DICOM cache (also run by compat.sh)
```

Machine-specific paths live in a gitignored `.env` at the repo root:

```bash
cp .env.example .env && $EDITOR .env      # DSA_DEVOPS, VOLVIEW_ROOT, ...
```

The stack must already be running (see `docs/development.md`) — `script/deploy`
only swaps the code it serves. `script/girder-volview.override.yml` re-points
the `/opt/girder_volview` mount at the worktree (or compat baseline) being
deployed; that's the only change this repo makes to the upstream stack.

What the harness needs beyond `.env`:

- **The baseline backend** — no checkout required. It's exported from this
  repo's own history at the sha pinned in `e2e/compat-baseline.json`. Override
  for a one-off with `COMPAT_BASELINE_REF=origin/main`, or point at a real git
  checkout with `COMPAT_OLD_CHECKOUT` + `COMPAT_OLD_SHA` (HEAD must equal that
  sha).
- **VolView worktrees** `main` and `just-jobs` under `VOLVIEW_ROOT` (override
  `COMPAT_BASELINE_VOLVIEW` / `COMPAT_BRANCH_VOLVIEW`). Baseline sha and
  published npm version are pinned in `e2e/compat-baseline.json`; the
  branch-side client is unpublished and builds from source, with its sha read
  from the worktree at run start. Set `COMPAT_BRANCH_VOLVIEW_SHA` to assert the
  checkout is at one particular commit.

To move the baseline forward, resolve the new sha and edit
`e2e/compat-baseline.json` — it's pinned rather than floating so a red compat
run is bisectable.

Full coverage-first run (two deploys):

```bash
cd e2e && npm test
```

`npm run compat` is an alias for the same run.

Iterating:

```bash
npm run compat:verify-fast    # verify only, no redeploy, keep state for re-runs
npm run compat:capture        # capture half only (deploys main first)
bash scripts/compat.sh --phase capture --skip-deploy   # re-capture, baseline already deployed
bash scripts/compat.sh --phase current --skip-deploy   # current scenarios using retained state
bash scripts/compat.sh --link                          # fast client deploys (docker cp, no npm pack)
COMPAT_BRANCH_VOLVIEW=/abs/path/to/VolView/just-jobs npm test  # another current checkout
npm run compat:clean          # remove materialized baselines (handles root-owned residue)
npm run report                # html report of the last phase
```

The capture phase refuses to start if `.compat-state.json` already exists,
meaning a previous capture was never verified or torn down. Either finish it
(`npm run compat:verify`) or delete the state file and the
`girder-volview-compat-<runId>` folder it names.

Optional full-devkit tier: seed the "VolView Devkit" collection first
(`e2e/seed/README.md`); the `devkit-study` gesture then runs automatically and
cleans up the session items it mints.

## Guards

- `verifyDeployedHeads` (e2e/helpers/stack.ts) requires the deployed backend
  and client shas to equal the intended checkouts. Capture runs against the
  baseline pair, so `compat.sh` passes `E2E_EXPECT_GIRDER_SHA` and
  `E2E_EXPECT_VOLVIEW_SHA`; verify checks this backend's HEAD and the current
  client HEAD. Outside compat it checks this worktree and the receipt's live
  VolView checkout. A missing or unreadable expected sha is a hard failure.
- The guard also hashes the served `index.html` and, when the receipt
  worktrees are locally accessible, compares the built client and Python
  source tree to the receipt — moving a checkout or changing code after deploy
  forces a fresh deploy instead of a false green.
- `script/deploy` writes the receipt only after confirming the served SPA and
  mounted backend match what it deployed, so a receipt never certifies a
  partial deploy.
- The baseline export has no `.git`, so its sha is asserted via `--girder-sha`
  and backstopped by comparing the mounted backend tree hash to the tree
  `materialize-baseline.sh` produced.
- One `playwright.config.ts` defines the capture, verify, and current
  projects, and refuses to start without the phase `compat.sh` selects —
  preventing a partial direct invocation from silently testing the wrong
  deploy.
- CI does not run this harness; it's a local tool by design. A future job
  needs the full DSA Compose stack plus a separate VolView checkout via
  `COMPAT_BRANCH_VOLVIEW` (and `COMPAT_BRANCH_VOLVIEW_SHA` for a reproducible
  cross-repository assertion).
