# VolView manual-test seed

Seeds three Girder collections with real public imaging data through an S3
import:

```text
Trial (MinIO Import)
├── patients/<patient>/<study>/{CT,PET}/
└── ultrasound/clip-{01,02,03}.dcm

Trial (Large Image Filter, MinIO Import)
├── patients/<patient>/<study>/{CT,PET}/
└── ultrasound/clip-{01,02,03}.dcm

Developer (MinIO Import)
├── prostate/{dicom/,5.seg.total-segmentator.nrrd}
├── fetus/{fetus.mha,fetus.seg.nrrd}
└── ultrasound/clip-{01,02,03}.dcm
```

The collection name carries the ingestion path: every imaging file in these
three arrived through the MinIO/S3 assetstore import, so a file's provenance is
readable from its breadcrumb without an extra folder level. The browser
compatibility harness uploads its fixtures directly instead, into the running
user's own folders — a different place entirely, never these collections. Keep
it that way: one study's slices should never span both paths.

The trial collections mirror one another: three patients, two studies per
patient, and CT plus PET in every study. Only the second collection receives
`.large_image_config.yaml`, in both `patients/` and `ultrasound/`. The Trial and
Developer ultrasound folders do not use large-image filtering.

For **Open Checked in VolView**, select `prostate/dicom` with its segmentation,
or select both files in `fetus`. The fetal segmentation is generated test data,
not a clinical annotation.

## Run

Start MinIO in the existing `dsa-plus` Compose project:

```bash
cp ../../.env.example ../../.env   # first time only
docker compose -p dsa-plus --env-file ../../.env -f docker-compose.minio.yml up -d
```

`--env-file` is load-bearing. Compose resolves `.env` against the compose file's
own directory, not the working directory, so without it the repo-root `.env` is
never read and `MINIO_DATA_DIR` silently falls back to `e2e/seed/.minio-data` —
an empty bucket, while girder still serves the file records imported from the
real one. A missing `.env` fails the command outright and names the path it
wanted, which is the intended behavior.

Set `MINIO_DATA_DIR` in that `.env` to share one seeded bucket across worktrees;
leave it unset for a single checkout.

Then prepare and seed:

```bash
uv run seed.py fetch
uv run seed.py stage
uv run seed.py seed
uv run seed.py verify
```

To clean and recreate all three managed collections while keeping the download
cache and staged MinIO objects:

```bash
uv run seed.py reseed
uv run seed.py verify
```

`reset` deletes the collections. Use `reset --bucket` to also empty MinIO.
`stage --max-slices N` controls the number of CT/PET instances per series; the
default is 40.

Upgrading from a seed that used the old unqualified collection names (`Trial`,
`Developer`) leaves those behind: `reset` only deletes the collections this
version names. Delete the old ones by hand, then `seed`.

No imaging data is committed. Downloads live under ignored `data/` storage and
are pinned by SeriesInstanceUID or SHA-512. See
[ATTRIBUTION.md](ATTRIBUTION.md) for sources and terms.
