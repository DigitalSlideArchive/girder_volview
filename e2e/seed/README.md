# VolView manual-test seed

Seeds three Girder collections with real public imaging data through an S3
import:

```text
Trial
└── patients/<patient>/<study>/{CT,PET}/

Trial (Large Image Filter)
└── patients/<patient>/<study>/{CT,PET}/

Developer
├── prostate/{dicom/,5.seg.total-segmentator.nrrd}
├── fetus/{fetus.mha,fetus.seg.nrrd}
└── ultrasound/clip-{01,02,03}.dcm
```

The trial collections mirror one another: three patients, two studies per
patient, and CT plus PET in every study. Only the second collection receives
`.large_image_config.yaml`.

For **Open Checked in VolView**, select `prostate/dicom` with its segmentation,
or select both files in `fetus`. The fetal segmentation is generated test data,
not a clinical annotation.

## Run

Start MinIO in the existing `dsa-plus` Compose project:

```bash
docker compose -p dsa-plus -f docker-compose.minio.yml up -d
```

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

No imaging data is committed. Downloads live under ignored `data/` storage and
are pinned by SeriesInstanceUID or SHA-512. See
[ATTRIBUTION.md](ATTRIBUTION.md) for sources and terms.
