# VolView e2e seed tool

Recreate a known-good DSA/Girder filesystem from scratch, seeded the way real
deployments ingest data — an **S3 bucket import** — so VolView's "click a
filtered DICOM row, get a study" flow can be tested reproducibly.

This directory is self-contained tooling: nothing in the `girder_volview`
package imports it. It seeds a *local* DSA stack for manual exercise and for
the optional devkit-collection tier of the e2e suite (see `../`).

Two structures get seeded:

```
VolView Devkit                       (Girder collection)
├── trial/                           clinical-trial hierarchy
│   ├── .large_image_config.yaml     Patient rows → Study rows → open in VolView
│   ├── .volview_config.yaml
│   ├── patient-01/
│   │   ├── study-01/{CT,PET}/
│   │   └── study-02/{CT,PET}/
│   ├── patient-02/ …
│   └── patient-03/ …
└── ultrasound/                      cine (multiframe) US clips
    ├── .large_image_config.yaml     one row per clip → open in VolView
    ├── .volview_config.yaml
    └── clip-01.dcm  clip-02.dcm  clip-03.dcm
```

The `.large_image_config.yaml` files are the point: without them a folder of
imported DICOM renders as a flat wall of per-slice items. With them, Girder's
hierarchy view groups into patient and study rows, and clicking a row hands the
whole study to VolView.

## Data and licensing

**No imaging data lives in this directory.** `manifest/series.json` pins
SeriesInstanceUIDs; `seed.py fetch` downloads them from the NCI Imaging Data
Commons public bucket (~560 MB). Only CC BY 3.0 / 4.0 series are selected, and
the license is checked per series — IDC licenses per *series*, not per
collection, so assuming by collection is a trap. See
[ATTRIBUTION.md](ATTRIBUTION.md) for the citations and terms that travel with
the data.

Sources:

| | Collection | Why |
|---|---|---|
| Trial | `acrin_nsclc_fdg_pet` (ACRIN 6668) | Baseline + post-treatment design, so patients really do have repeat CT+PET studies. |
| Cine US | `b_mode_and_ceus_liver` | Verified 35–41 frame B-mode/contrast liver loops. |

Two collections that look right and aren't: **CMB-LCA** has only 13 CT+PET
studies and no patient with more than one, so a 3×2 trial hierarchy can't be
built from it; and the CMB ultrasound series are single-frame stills, while
`prostate_mri_us_biopsy` is multiframe but a 3D volume stack, not a temporal
loop.

## Setup

Needs [`uv`](https://github.com/astral-sh/uv), Docker, and a running `dsa-plus`
stack (`../../script/deploy`, with machine paths set in the repo-root `.env`
— see `.env.example`).

Bring up MinIO **into the existing compose project**, so it shares a network with
the girder container:

```bash
docker compose -p dsa-plus -f docker-compose.minio.yml up -d
```

Girder then reaches it at `http://minio:9000`; from your host it's
`localhost:9000` (console on `localhost:9001`, `minioadmin`/`minioadmin`).

## Use

```bash
uv run seed.py fetch                  # download pinned series → data/
uv run seed.py stage                  # arrange + push to MinIO
uv run seed.py seed                   # assetstore + import + metadata + configs
uv run seed.py verify                 # assert it all works
```

Then open the collection in Girder and click into `trial/`.

Other commands:

| Command | |
|---|---|
| `select` | Re-query IDC and re-pin `manifest/series.json`. Rarely needed. |
| `fetch --small` | Download only the three small-tier series (~a few MB of slices used). |
| `stage --max-slices N` | Slices per CT/PET series (default 40). |
| `seed-small --folder-id ID [--slices N]` | Plain-upload the small-tier slices (no MinIO) into a folder, with `meta.dicom.*` set. Used by the e2e compat provisioning. |
| `reset [--bucket]` | Delete the collection and assetstore; optionally empty the bucket. |

The small tier is `patient-01/study-01/{CT,PET}` (a layerable CT+PET study) plus
`patient-02/study-01/CT`, subsampled to a few slices per series — real IDC DICOM
without the full ~560 MB fetch or the MinIO import path.

Migrating from the standalone `girder-volview-devkit` repo? Copy its download
cache to skip the re-fetch: `cp -r <old-repo>/data e2e/seed/data`.

Everything is idempotent — re-running `stage` and `seed` reuses existing folders
and items rather than duplicating them.

Overridable via env: `GIRDER_URL`, `DSA_ADMIN_USER`, `DSA_ADMIN_PASS`,
`MINIO_HOST_URL`, `MINIO_GIRDER_URL`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`,
`DEVKIT_BUCKET`.

## Things that will bite you

**S3 import creates one Girder item per key.** The adapter ignores
`leafFoldersAsItems`, so a 200-slice series is 200 items. That's why
`--max-slices` exists and why the configs group by series.

**`fileIncludeRegex` is matched with `re.match`, not `re.search`.** It's anchored
at the start of the basename, so `\.dcm$` matches nothing and the import silently
creates folders with no items. Use `.*\.dcm$`.

**An S3 import fires no `data.process` event.** It never routes through
`Upload.finalizeUpload`, so the `dicom_viewer` plugin never runs and *nothing*
populates DICOM metadata. `seed` therefore reads tags locally with pydicom and
writes them to each item as `meta.dicom.*` — which is what the grouping actually
queries. Note `POST /item/:id/parseDicom` would **not** be enough: it writes
`item['dicom']`, while large_image groups on `item['meta']['dicom']`.

**`large_image.auto_set` must be off during import.** Import does fire
`model.file.save`, which walks into large_image's DICOM adjacency scan — a
documented O(n²) blowup (`../../plans/DICOM_S3_IMPORT_ROOT_CAUSE.md`, 2 s → 70 s
per file). `seed` disables it and restores the previous value afterwards. If an
import crawls, check this before blaming the data.

**The VolView config resolves by item name.** `girder_volview` matches an item
name against the URL segment, and both the folder manifest and the "open in
VolView" link request `.volview_config.yaml`. Get that name wrong and the
endpoint quietly serves only `BASE_CONFIG` — the config appears uploaded and
does nothing. `verify` compares the *served* config against the local file for
exactly this reason.

**VolView drops invalid config values silently.** `disabledViewTypes` accepts
only `2D`, `3D`, `Oblique`; layout view names only `axial`, `coronal`,
`sagittal`, `volume`, `oblique` (lowercase). Anything else is stripped by zod
with no warning, so a plausible guess like `Coronal` looks applied and isn't.
`verify` checks the configs against those value sets.

**Which layout opens is decided by name, not by a setting.** There's no
"defaultLayout" key, but you can still control it: VolView switches to whichever
layout is *first* in the map it receives (`configJson.ts` `applyLayout` →
`layoutEntries[0][0]`). Two things determine that, and neither is the order you
wrote the YAML in:

1. Girder serializes the config response with keys **sorted alphabetically**, so
   authoring order is discarded — the alphabetically first *name* wins. That's
   why the ultrasound layout is called `1 Cine (single pane)`.
2. `BASE_CONFIG` contributes its own `Axial Coronal Sagittal`. Setting
   `__all__: true` inside `layouts` makes girder_volview's merge clear the base
   map first, so only your layouts are offered.

This is how the ultrasound folder opens straight into one full-width pane instead
of two junk panes reslicing along the time axis. `verify` pins the expected
active layout per folder so a rename can't silently change it.

**Presigned-URL redirects.** Girder's S3 adapter would normally redirect
downloads to `http://minio:9000/…`, which your browser can't resolve.
`girder_volview` sidesteps this by streaming instead (`volview.proxy_assetstores`,
default on). `verify` asserts a 200 rather than a 303, since flipping that config
off would break VolView silently.
