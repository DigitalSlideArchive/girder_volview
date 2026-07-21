# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "idc-index",
#     "pydicom",
#     "boto3",
#     "girder-client",
#     "pyyaml",
#     "requests",
# ]
# ///
"""
Seed a local DSA/Girder with public CC-BY DICOM via a simulated S3 import.

Pipeline (each step is idempotent and re-runnable):

    uv run seed.py select     # query IDC -> manifest/series.json  (rare)
    uv run seed.py fetch      # download pinned series -> data/    + ATTRIBUTION.md
    uv run seed.py stage      # arrange into bucket layout -> MinIO
    uv run seed.py seed       # assetstore + import + metadata + configs
    uv run seed.py verify     # assert the whole thing actually works
    uv run seed.py reset      # tear down the Girder side

Data is NOT vendored: `select` pins SeriesInstanceUIDs, `fetch` downloads them
from IDC's public bucket. See ATTRIBUTION.md for the terms that ride along.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent.resolve()
DATA_DIR = HERE / "data"
MANIFEST_PATH = HERE / "manifest" / "series.json"
STAGED_PATH = DATA_DIR / ".staged.json"
CONFIGS_DIR = HERE / "configs"
ATTRIBUTION_PATH = HERE / "ATTRIBUTION.md"

GIRDER_URL = os.environ.get("GIRDER_URL", "http://localhost:8080")
API_ROOT = GIRDER_URL.rstrip("/") + "/api/v1"
ADMIN_USER = os.environ.get("DSA_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("DSA_ADMIN_PASS", "password")

MINIO_HOST_URL = os.environ.get("MINIO_HOST_URL", "http://localhost:9000")
# What Girder (inside the compose network) uses to reach MinIO. Not the same as
# the host URL above -- the container cannot resolve "localhost:9000".
MINIO_GIRDER_URL = os.environ.get("MINIO_GIRDER_URL", "http://minio:9000")
MINIO_KEY = os.environ.get("MINIO_ROOT_USER", "minioadmin")
MINIO_SECRET = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
BUCKET = os.environ.get("DEVKIT_BUCKET", "dsa-devkit")

VOLVIEW_CONFIG_NAME = ".volview_config.yaml"

# VolView validates its config with zod and silently drops values that don't
# match, so an invalid-but-plausible entry looks like it applied and does
# nothing. Mirrored from src/io/import/configJson.ts and utils/layoutParsing.ts.
VALID_DISABLED_VIEW_TYPES = {"2D", "3D", "Oblique"}
VALID_LAYOUT_VIEWS = {"axial", "coronal", "sagittal", "volume", "oblique"}

# VolView opens whichever layout comes first in the map it receives, and Girder
# serializes config keys alphabetically -- so the layout that opens is decided by
# NAME, not by the order written in the YAML. Pin the intent so a rename can't
# silently change which layout users land in.
EXPECTED_ACTIVE_LAYOUT = {
    "trial": "Axial Coronal Sagittal",
    "ultrasound": "1 Cine (single pane)",
}

COLLECTION_NAME = "VolView Devkit"
ASSETSTORE_NAME = "Devkit MinIO"
ASSETSTORE_TYPE_S3 = 2  # girder.constants.AssetstoreType.S3

# Only these are safe to redistribute. IDC licenses per SERIES, not per
# collection, so this is filtered on every pick rather than assumed.
ALLOWED_LICENSES = ("CC BY 4.0", "CC BY 3.0")

# ACRIN 6668: FDG-PET/CT of NSCLC with an explicit baseline + post-treatment
# design, so patients genuinely have repeat CT+PET studies. CMB-LCA looks like an
# obvious choice but only 13 of its studies have both CT and PT, and no patient
# has more than one -- so a 3x2 trial hierarchy is impossible there.
TRIAL_COLLECTION = "acrin_nsclc_fdg_pet"

# Real cine: verified 35-41 frame color loops from GE scanners. The CMB
# ultrasound is single-frame stills and prostate_mri_us_biopsy is a 3D volume
# stack (multiframe, but not a temporal loop), so neither is what we want here.
US_COLLECTION = "b_mode_and_ceus_liver"

N_PATIENTS = 3
N_STUDIES = 2
N_CLIPS = 3

# The e2e "small tier": a couple of real multi-file series uploaded directly
# (no MinIO) into a test folder — one study whose CT+PET pair exercises
# PET-over-CT layering, plus a second patient's CT so filtering has something
# to exclude. (patient_slot, study_slot, modality_slot) of manifest entries.
SMALL_TIER_SLOTS = {
    ("patient-01", "study-01", "CT"),
    ("patient-01", "study-01", "PET"),
    ("patient-02", "study-01", "CT"),
}

# Skip scouts/topograms and keep each series small enough to seed quickly.
CT_MIN_INSTANCES = 40
CT_SIZE_MB = (5, 90)
PT_SIZE_MB = (2, 60)
US_MAX_SERIES_MB = 400
# Instance size is what separates a cine loop from a still in this collection:
# a multi-frame loop is tens of MB, a single-frame still is well under one.
US_MIN_MB_PER_INSTANCE = 20

# DICOM tags copied onto each Girder item as meta.dicom.* -- these are what the
# .large_image_config.yaml files group and sort on.
META_TAGS = [
    "PatientID",
    "PatientName",
    "PatientSex",
    "PatientAge",
    "StudyInstanceUID",
    "StudyDescription",
    "StudyDate",
    "SeriesInstanceUID",
    "SeriesDescription",
    "SeriesNumber",
    "Modality",
    "Manufacturer",
    "ManufacturerModelName",
    "NumberOfFrames",
    "ModalitiesInStudy",
    # Cine clips share a SeriesInstanceUID, so the ultrasound view groups on this
    # instead to get one row per clip.
    "SOPInstanceUID",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def stack_up(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(API_ROOT + "/system/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def minio_up(timeout: float = 3.0) -> bool:
    # MinIO answers /minio/health/live without credentials.
    try:
        url = MINIO_HOST_URL.rstrip("/") + "/minio/health/live"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def read_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        die(f"No manifest at {MANIFEST_PATH}. Run `seed.py select` first.")
    return json.loads(MANIFEST_PATH.read_text())


def girder_client():
    from girder_client import GirderClient

    if not stack_up():
        die(f"Girder not reachable at {API_ROOT}. Is the dsa-plus stack up?")
    gc = GirderClient(apiUrl=API_ROOT)
    gc.authenticate(ADMIN_USER, ADMIN_PASS)
    return gc


def s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=MINIO_HOST_URL,
        aws_access_key_id=MINIO_KEY,
        aws_secret_access_key=MINIO_SECRET,
        region_name="us-east-1",
    )


def pick_trial_series(idx):
    """3 patients x 2 studies, each study contributing one CT and one PET series.

    Returns a list of pick dicts. Pure w.r.t. the dataframe -- no I/O.
    """
    pool = idx[
        (idx.collection_id == TRIAL_COLLECTION)
        & (idx.license_short_name.isin(ALLOWED_LICENSES))
    ]
    ct = pool[
        (pool.Modality == "CT")
        & (pool.instanceCount >= CT_MIN_INSTANCES)
        & pool.series_size_MB.between(*CT_SIZE_MB)
    ]
    pt = pool[
        (pool.Modality == "PT")
        & (pool.instanceCount >= CT_MIN_INSTANCES)
        & pool.series_size_MB.between(*PT_SIZE_MB)
    ]

    both = set(ct.StudyInstanceUID) & set(pt.StudyInstanceUID)
    if not both:
        die(f"No {TRIAL_COLLECTION} study has both a CT and a PT series in bounds.")

    pairs = (
        pool[pool.StudyInstanceUID.isin(both)][
            ["PatientID", "StudyInstanceUID", "StudyDate"]
        ]
        .drop_duplicates(subset=["PatientID", "StudyInstanceUID"])
        .sort_values(["PatientID", "StudyDate"])
    )
    longitudinal = pairs.groupby("PatientID").filter(lambda g: len(g) >= N_STUDIES)
    if longitudinal.empty:
        die(f"No {TRIAL_COLLECTION} patient has >= {N_STUDIES} CT+PET studies.")

    picks = []
    patients = list(longitudinal.groupby("PatientID"))[:N_PATIENTS]
    if len(patients) < N_PATIENTS:
        log(
            f"  warning: only {len(patients)} qualifying patients "
            f"(wanted {N_PATIENTS})"
        )

    for p_i, (_patient_id, group) in enumerate(patients, start=1):
        studies = list(group.StudyInstanceUID)[:N_STUDIES]
        for s_i, study_uid in enumerate(studies, start=1):
            for modality, source in (("CT", ct), ("PET", pt)):
                rows = source[source.StudyInstanceUID == study_uid]
                if rows.empty:
                    continue
                # Smallest qualifying series keeps the seed quick.
                row = rows.sort_values("series_size_MB").iloc[0]
                picks.append(
                    {
                        "patient_slot": f"patient-{p_i:02d}",
                        "study_slot": f"study-{s_i:02d}",
                        "modality_slot": modality,
                        "PatientID": str(row.PatientID),
                        "StudyInstanceUID": str(row.StudyInstanceUID),
                        "SeriesInstanceUID": str(row.SeriesInstanceUID),
                        "Modality": str(row.Modality),
                        "SeriesDescription": str(row.SeriesDescription),
                        "collection_id": str(row.collection_id),
                        "source_DOI": str(row.source_DOI),
                        "license_short_name": str(row.license_short_name),
                        "series_size_MB": float(row.series_size_MB),
                        "instanceCount": int(row.instanceCount),
                    }
                )
    return picks


def pick_us_series(idx):
    """One cine-ultrasound series; each of its instances becomes a clip.

    The index has no NumberOfFrames column, so cine-ness cannot be confirmed
    here -- `fetch` proves it with pydicom and fails loudly if a pick turns out
    to be a still. The proxy is average instance size: these series mix cine
    loops (tens of MB) with single-frame stills (well under 1 MB), so a large
    mean instance means the series is loops. Among those, take the smallest
    total, since downloads are whole-series.
    """
    us = idx[
        (idx.collection_id == US_COLLECTION)
        & (idx.Modality == "US")
        & idx.license_short_name.isin(ALLOWED_LICENSES)
        & (idx.instanceCount >= N_CLIPS)
        & (idx.series_size_MB <= US_MAX_SERIES_MB)
    ].copy()

    us = us[us.series_size_MB / us.instanceCount >= US_MIN_MB_PER_INSTANCE]
    if us.empty:
        die(
            f"No {US_COLLECTION} series with >= {N_CLIPS} instances averaging "
            f">= {US_MIN_MB_PER_INSTANCE} MB under {US_MAX_SERIES_MB} MB total."
        )

    row = us.sort_values("series_size_MB").iloc[0]

    return [
        {
            "clip_count": N_CLIPS,
            "PatientID": str(row.PatientID),
            "StudyInstanceUID": str(row.StudyInstanceUID),
            "SeriesInstanceUID": str(row.SeriesInstanceUID),
            "Modality": "US",
            "SeriesDescription": str(row.SeriesDescription),
            "collection_id": str(row.collection_id),
            "source_DOI": str(row.source_DOI),
            "license_short_name": str(row.license_short_name),
            "series_size_MB": float(row.series_size_MB),
            "instanceCount": int(row.instanceCount),
        }
    ]


def cmd_select(args) -> None:
    from idc_index import IDCClient

    log("Loading the IDC index (first run downloads ~77 MB and takes a moment)...")
    client = IDCClient()
    idx = client.index
    log(f"  index: {len(idx):,} series")

    log(f"Selecting CT+PET trial series from {TRIAL_COLLECTION}...")
    trial = pick_trial_series(idx)
    n_patients = len({p["patient_slot"] for p in trial})
    log(f"  {len(trial)} series across {n_patients} patients")

    log("Selecting a cine-ultrasound series...")
    ultrasound = pick_us_series(idx)
    for p in ultrasound:
        log(
            f"  {p['PatientID']}: {p['instanceCount']} instances, "
            f"{p['series_size_MB']:.0f} MB -> {p['clip_count']} clips"
        )

    bad = [
        p for p in trial + ultrasound if p["license_short_name"] not in ALLOWED_LICENSES
    ]
    if bad:
        die(f"{len(bad)} picks are not CC-BY -- refusing to write the manifest.")

    manifest = {
        "note": "Pinned IDC series. Regenerate with `seed.py select`.",
        "allowed_licenses": list(ALLOWED_LICENSES),
        "trial": trial,
        "ultrasound": ultrasound,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    total = sum(p["series_size_MB"] for p in trial + ultrasound)
    n_series = len(trial) + len(ultrasound)
    log(f"\nWrote {MANIFEST_PATH} ({n_series} series, ~{total:.0f} MB)")


def series_dir(series_uid: str) -> Path:
    return DATA_DIR / series_uid


def small_tier_picks(manifest: dict) -> list[dict]:
    picks = [
        p
        for p in manifest["trial"]
        if (p["patient_slot"], p["study_slot"], p["modality_slot"]) in SMALL_TIER_SLOTS
    ]
    if len(picks) != len(SMALL_TIER_SLOTS):
        die(
            f"Manifest lacks the small-tier series {sorted(SMALL_TIER_SLOTS)}; "
            "re-run `seed.py select`?"
        )
    return picks


def cmd_fetch(args) -> None:
    from idc_index import IDCClient

    manifest = read_manifest()
    small = getattr(args, "small", False)
    picks = (
        small_tier_picks(manifest)
        if small
        else manifest["trial"] + manifest["ultrasound"]
    )

    wanted = [p["SeriesInstanceUID"] for p in picks]
    missing = [uid for uid in wanted if not list(series_dir(uid).glob("*.dcm"))]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if missing and not args.force:
        log(
            f"Downloading {len(missing)} series from IDC "
            f"({len(wanted) - len(missing)} already present)..."
        )
    elif args.force:
        missing = wanted
        log(f"Re-downloading all {len(missing)} series (--force)...")
    else:
        log("All pinned series already present.")

    if missing:
        client = IDCClient()
        client.download_from_selection(
            seriesInstanceUID=missing,
            downloadDir=str(DATA_DIR),
            dirTemplate="%SeriesInstanceUID",
            dry_run=False,
            show_progress_bar=True,
        )

    verify_fetch(picks)
    if small:
        # ATTRIBUTION.md documents the FULL pinned set; regenerating it from a
        # three-series subset would shrink the committed citations.
        log("Skipping ATTRIBUTION.md regeneration (--small subset).")
    else:
        write_attribution(picks)


def verify_fetch(picks: list[dict]) -> None:
    """Assert every series landed, and that US picks are genuinely multiframe."""
    import pydicom

    log("\nVerifying downloads...")
    problems = []
    for p in picks:
        files = sorted(series_dir(p["SeriesInstanceUID"]).glob("*.dcm"))
        if not files:
            problems.append(f"{p['SeriesInstanceUID']}: no files downloaded")
            continue
        if p["license_short_name"] not in ALLOWED_LICENSES:
            problems.append(
                f"{p['SeriesInstanceUID']}: license {p['license_short_name']}"
            )
        if p["Modality"] == "US":
            # Every instance we intend to stage as a clip must really be cine.
            for i, path in enumerate(files[: p["clip_count"]], start=1):
                ds = pydicom.dcmread(path, stop_before_pixels=True)
                frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
                if frames <= 1:
                    problems.append(
                        f"{path.name}: US but NumberOfFrames={frames} "
                        "(not a cine loop -- re-run `select`)"
                    )
                else:
                    log(f"  clip-{i:02d}: {frames} frames OK")
        else:
            slot = f"{p['patient_slot']}/{p['study_slot']}/{p['modality_slot']}"
            log(f"  {slot}: {len(files)} slices")

    if problems:
        for problem in problems:
            print(f"  FAIL {problem}", file=sys.stderr)
        die(f"{len(problems)} series failed verification.")
    log("All series verified.")


def clean_citation(text: str) -> str:
    """IDC returns APA citations as HTML; flatten them for markdown."""
    import html
    import re

    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(text)))).strip()


def write_attribution(picks: list[dict]) -> None:
    """Regenerate ATTRIBUTION.md. Citations come from IDC, not a hand-kept list."""
    from idc_index import IDCClient

    log("\nGenerating ATTRIBUTION.md...")
    client = IDCClient()
    try:
        citations = client.citations_from_selection(
            seriesInstanceUID=[p["SeriesInstanceUID"] for p in picks]
        )
    except Exception as exc:  # network/API hiccup shouldn't lose the download
        log(f"  warning: could not fetch citations ({exc}); falling back to DOIs")
        citations = sorted({p["source_DOI"] for p in picks})

    if isinstance(citations, (list, tuple)):
        citations_text = "\n".join(f"- {clean_citation(c)}" for c in citations)
    else:
        citations_text = clean_citation(str(citations))

    dois = sorted({p["source_DOI"] for p in picks})
    licenses = sorted({p["license_short_name"] for p in picks})

    ATTRIBUTION_PATH.write_text(
        f"""# Attribution

The DICOM data this devkit downloads comes from the NCI Imaging Data Commons
(IDC) and is redistributed under {" / ".join(licenses)}. No imaging data is
stored in this repository -- `seed.py fetch` pulls it from IDC's public bucket.

## Datasets used

{citations_text}

DOIs: {", ".join(dois)}

## IDC

Fedorov, A., Longabaugh, W. J. R., Pot, D., et al. *National Cancer Institute
Imaging Data Commons: Toward Transparency, Reproducibility, and Scalability in
Imaging Artificial Intelligence.* RadioGraphics (2023).
https://doi.org/10.1148/rg.230180

## Terms that travel with this data

Per the TCIA Data Usage Policy
(https://www.cancerimagingarchive.net/data-usage-policies-and-restrictions/):

- Attribute each individual dataset used, and link to that policy.
- Pass this same obligation on to downstream users.
- Do not attempt to identify or contact the individuals these images came from.

Regenerate this file with `uv run seed.py fetch`.
""",
    )
    log(f"  wrote {ATTRIBUTION_PATH}")


def sorted_slices(files: list[Path]) -> list[Path]:
    """Order a series by InstanceNumber so subsampling stays anatomically sane."""
    import pydicom

    def key(path: Path):
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True)
            return (int(getattr(ds, "InstanceNumber", 0) or 0), path.name)
        except Exception:
            return (0, path.name)

    return sorted(files, key=key)


def subsample(files: list[Path], limit: int) -> list[Path]:
    """Take at most `limit` files at a uniform stride.

    Uniform stride keeps slice spacing constant, so the volume stays valid --
    just coarser. Taking the first N would truncate the anatomy instead.
    """
    if limit <= 0 or len(files) <= limit:
        return files
    stride = len(files) / limit
    return [files[int(i * stride)] for i in range(limit)]


def file_metadata(path: Path) -> dict:
    """DICOM tags for one file, destined for that item's meta.dicom.*."""
    import pydicom

    ds = pydicom.dcmread(path, stop_before_pixels=True)
    meta = {}
    for tag in META_TAGS:
        value = getattr(ds, tag, None)
        if value is None or value == "":
            continue
        # Only true multi-valued elements become lists. Testing for __iter__
        # instead would explode a PersonName into a list of characters, since
        # pydicom's PersonName iterates per character.
        if isinstance(value, pydicom.multival.MultiValue):
            value = [str(v) for v in value]
        else:
            value = str(value)
        meta[tag] = value
    return meta


def build_staging_plan(manifest: dict, max_slices: int) -> dict:
    """manifest + local files -> the exact set of objects to upload.

    Metadata is captured per object rather than per series: the cine clips all
    share one SeriesInstanceUID and are told apart by SOPInstanceUID, which the
    ultrasound view groups on.
    """
    objects = []

    # ModalitiesInStudy is a query-level attribute, absent from the instances
    # themselves, so the study view's Modalities column would render empty.
    # Derive it from what we actually staged into each study.
    study_modalities: dict[str, set] = {}
    for p in manifest["trial"]:
        study_modalities.setdefault(p["StudyInstanceUID"], set()).add(p["Modality"])

    for p in manifest["trial"]:
        uid = p["SeriesInstanceUID"]
        files = sorted_slices(list(series_dir(uid).glob("*.dcm")))
        if not files:
            die(f"Missing local files for {uid}. Run `seed.py fetch`.")
        prefix = f"trial/{p['patient_slot']}/{p['study_slot']}/{p['modality_slot']}"
        modalities = sorted(study_modalities[p["StudyInstanceUID"]])
        for i, path in enumerate(subsample(files, max_slices)):
            meta = file_metadata(path)
            meta["ModalitiesInStudy"] = modalities
            objects.append(
                {"key": f"{prefix}/{i:04d}.dcm", "local": str(path), "meta": meta}
            )

    for p in manifest["ultrasound"]:
        uid = p["SeriesInstanceUID"]
        files = sorted(series_dir(uid).glob("*.dcm"))
        if not files:
            die(f"Missing local files for {uid}. Run `seed.py fetch`.")
        for i, path in enumerate(files[: p["clip_count"]], start=1):
            objects.append(
                {
                    "key": f"ultrasound/clip-{i:02d}.dcm",
                    "local": str(path),
                    "meta": file_metadata(path),
                }
            )

    return {"bucket": BUCKET, "objects": objects}


def cmd_stage(args) -> None:
    if not minio_up():
        die(
            f"MinIO not reachable at {MINIO_HOST_URL}.\n"
            "  Start it with: docker compose -p dsa-plus "
            f"-f {HERE / 'docker-compose.minio.yml'} up -d"
        )

    manifest = read_manifest()
    log(f"Building staging plan (max {args.max_slices} slices per series)...")
    plan = build_staging_plan(manifest, args.max_slices)
    log(f"  {len(plan['objects'])} objects")

    s3 = s3_client()
    existing_buckets = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if BUCKET not in existing_buckets:
        log(f"Creating bucket {BUCKET}")
        s3.create_bucket(Bucket=BUCKET)

    already = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        already.update(o["Key"] for o in page.get("Contents", []))

    uploaded = 0
    for obj in plan["objects"]:
        if obj["key"] in already and not args.force:
            continue
        s3.upload_file(
            obj["local"],
            BUCKET,
            obj["key"],
            ExtraArgs={"ContentType": "application/dicom"},
        )
        uploaded += 1
        if uploaded % 25 == 0:
            log(f"  uploaded {uploaded}...")

    STAGED_PATH.write_text(json.dumps(plan, indent=2) + "\n")
    already = len(plan["objects"]) - uploaded
    log(f"Uploaded {uploaded} objects ({already} already present).")
    log(f"Wrote staging plan to {STAGED_PATH}")


def get_setting(gc, key: str):
    return gc.get("system/setting", parameters={"key": key})


def set_setting(gc, key: str, value) -> None:
    gc.put("system/setting", parameters={"key": key, "value": json.dumps(value)})


def ensure_collection(gc, name: str) -> dict:
    for coll in gc.listCollection():
        if coll["name"] == name:
            return coll
    return gc.post("collection", parameters={"name": name, "public": "true"})


def ensure_folder(gc, parent_id: str, parent_type: str, name: str) -> dict:
    return gc.createFolder(
        parent_id, name, parentType=parent_type, reuseExisting=True, public=True
    )


def ensure_assetstore(gc) -> dict:
    for store in gc.get("assetstore"):
        if store["name"] == ASSETSTORE_NAME:
            return store
    log(f"Creating S3 assetstore {ASSETSTORE_NAME!r} -> {MINIO_GIRDER_URL}/{BUCKET}")
    # Girder probes the bucket with a real put_object here, so the bucket must
    # already exist -- `stage` runs first.
    return gc.post(
        "assetstore",
        parameters={
            "type": ASSETSTORE_TYPE_S3,
            "name": ASSETSTORE_NAME,
            "bucket": BUCKET,
            "prefix": "",
            "accessKeyId": MINIO_KEY,
            "secret": MINIO_SECRET,
            "service": MINIO_GIRDER_URL,
            "region": "us-east-1",
        },
    )


def import_prefix(gc, assetstore_id: str, prefix: str, folder_id: str) -> None:
    log(f"Importing s3://{BUCKET}/{prefix} -> folder {folder_id}")
    gc.post(
        f"assetstore/{assetstore_id}/import",
        parameters={
            "importPath": prefix,
            "destinationId": folder_id,
            "destinationType": "folder",
            "progress": True,
            # Anchored: Girder matches this with re.match against the basename,
            # not re.search, so a bare r"\.dcm$" silently imports nothing.
            "fileIncludeRegex": r".*\.dcm$",
        },
    )


def folder_path_index(gc, root_id: str) -> dict:
    """Map relative folder path -> folder id, walking down from root."""
    index = {(): root_id}

    def walk(folder_id: str, parts: tuple):
        for child in gc.listFolder(folder_id, parentFolderType="folder"):
            key = parts + (child["name"],)
            index[key] = child["_id"]
            walk(child["_id"], key)

    walk(root_id, ())
    return index


def apply_metadata(gc, plan: dict, roots: dict) -> int:
    """Copy series-level DICOM tags onto every imported item as meta.dicom.*.

    An S3 import never routes through Upload.finalizeUpload, so `data.process`
    never fires and nothing populates DICOM metadata. The hierarchy-view grouping
    queries meta.dicom.*, so without this the folders render as a flat file list.
    """
    log("Applying meta.dicom.* to imported items...")

    # folder id -> {item name: item id}, filled lazily.
    item_cache: dict[str, dict] = {}
    path_cache: dict[str, dict] = {}
    applied, orphaned = 0, []

    for obj in plan["objects"]:
        parts = obj["key"].split("/")
        top, rel_dirs, filename = parts[0], parts[1:-1], parts[-1]
        root_id = roots.get(top)
        if root_id is None:
            continue

        if top not in path_cache:
            path_cache[top] = folder_path_index(gc, root_id)
        folder_id = path_cache[top].get(tuple(rel_dirs))
        if folder_id is None:
            orphaned.append(obj["key"])
            continue

        if folder_id not in item_cache:
            item_cache[folder_id] = {
                item["name"]: item["_id"] for item in gc.listItem(folder_id)
            }
        item_id = item_cache[folder_id].get(filename)
        if item_id is None:
            orphaned.append(obj["key"])
            continue

        gc.addMetadataToItem(item_id, {"dicom": obj["meta"]})
        applied += 1
        if applied % 50 == 0:
            log(f"  {applied}...")

    if orphaned:
        log(f"  warning: {len(orphaned)} staged objects had no matching item")
        for key in orphaned[:5]:
            log(f"    {key}")
    log(f"  applied metadata to {applied} items")
    return applied


def upload_config(gc, folder_id: str, path: Path, item_name: str | None = None) -> None:
    item = gc.createItem(folder_id, item_name or path.name, reuseExisting=True)
    # Replace rather than append, so re-seeding picks up edited configs.
    for existing in gc.listFile(item["_id"]):
        gc.delete(f"file/{existing['_id']}")
    with open(path, "rb") as fh:
        gc.uploadFile(
            parentId=item["_id"],
            stream=fh,
            name=item_name or path.name,
            size=path.stat().st_size,
            parentType="item",
            mimeType="application/x-yaml",
        )


def cmd_seed(args) -> None:
    if not STAGED_PATH.exists():
        die(f"No staging plan at {STAGED_PATH}. Run `seed.py stage` first.")
    plan = json.loads(STAGED_PATH.read_text())

    gc = girder_client()

    # The import path fires model.file.save per file, which walks into
    # large_image's DICOM adjacency scan -- an O(n^2) blowup on large series.
    # Off during import, restored after.
    previous_auto_set = get_setting(gc, "large_image.auto_set")
    log(f"Disabling large_image.auto_set during import (was {previous_auto_set!r})")
    set_setting(gc, "large_image.auto_set", False)

    try:
        collection = ensure_collection(gc, COLLECTION_NAME)
        log(f"Collection {COLLECTION_NAME!r}: {collection['_id']}")

        roots = {}
        for prefix in ("trial", "ultrasound"):
            folder = ensure_folder(gc, collection["_id"], "collection", prefix)
            roots[prefix] = folder["_id"]

        assetstore = ensure_assetstore(gc)
        for prefix, folder_id in roots.items():
            import_prefix(gc, assetstore["_id"], f"{prefix}/", folder_id)

        apply_metadata(gc, plan, roots)

        log("Uploading config files...")
        for prefix, folder_id in roots.items():
            for config in sorted((CONFIGS_DIR / prefix).glob(".*.yaml")):
                upload_config(gc, folder_id, config)
                log(f"  {prefix}/{config.name}")
    finally:
        set_setting(gc, "large_image.auto_set", previous_auto_set)
        log(f"Restored large_image.auto_set to {previous_auto_set!r}")

    log(f"\nSeeded. Open {GIRDER_URL}/#collection/{collection['_id']}")


def cmd_seed_small(args) -> None:
    """Upload the small-tier slices straight into a folder (no MinIO/S3).

    The e2e compat provisioning calls this against its own run folder: a couple
    of multi-file series (patient-01 study-01 CT+PET for layering, patient-02
    study-01 CT for filtering) at --slices per series, flat item names, and
    meta.dicom.* written the same way `seed` does — plain uploads fire
    data.process, but nothing in this stack populates DICOM metadata from it.
    """
    manifest = read_manifest()
    picks = small_tier_picks(manifest)

    missing = [
        p["SeriesInstanceUID"]
        for p in picks
        if not list(series_dir(p["SeriesInstanceUID"]).glob("*.dcm"))
    ]
    if missing:
        die(
            f"{len(missing)} small-tier series not downloaded. "
            "Run `seed.py fetch --small` first."
        )

    gc = girder_client()
    existing = {item["name"]: item["_id"] for item in gc.listItem(args.folder_id)}

    study_modalities: dict[str, set] = {}
    for p in picks:
        study_modalities.setdefault(p["StudyInstanceUID"], set()).add(p["Modality"])

    # Plain uploads fire model.file.save, so with large_image.auto_set on these
    # slices become bioformats tile sources -- which then hang the grouped item
    # list at view time (and trip the O(n^2) DICOM adjacency scan). The devkit's
    # `seed` disables auto_set for the same reason; do the same here.
    previous_auto_set = get_setting(gc, "large_image.auto_set")
    set_setting(gc, "large_image.auto_set", False)

    uploaded, skipped = 0, 0
    try:
        for p in picks:
            uid = p["SeriesInstanceUID"]
            files = sorted_slices(list(series_dir(uid).glob("*.dcm")))
            slot = f"{p['patient_slot']}-{p['study_slot']}-{p['modality_slot']}"
            modalities = sorted(study_modalities[p["StudyInstanceUID"]])
            for i, path in enumerate(subsample(files, args.slices)):
                name = f"{slot}-{i:04d}.dcm"
                if name in existing:
                    skipped += 1
                    continue
                meta = file_metadata(path)
                meta["ModalitiesInStudy"] = modalities
                file_doc = gc.uploadFileToFolder(
                    args.folder_id,
                    str(path),
                    filename=name,
                    mimeType="application/dicom",
                )
                gc.addMetadataToItem(file_doc["itemId"], {"dicom": meta})
                uploaded += 1
            log(f"  {slot}: {min(len(files), args.slices)} slices")
    finally:
        set_setting(gc, "large_image.auto_set", previous_auto_set)

    log(
        f"Seeded small tier into folder {args.folder_id} "
        f"({uploaded} uploaded, {skipped} existing)."
    )


def collect_layout_views(layouts: dict) -> set:
    """Every bare view name mentioned across a layouts block."""

    def views(node) -> set:
        if isinstance(node, str):
            return {node}
        if isinstance(node, list):
            return set().union(set(), *(views(n) for n in node))
        if isinstance(node, dict):
            # Recurse only into `items`; `direction` holds row/column, not views.
            return views(node.get("items", []))
        return set()

    return views(list(layouts.values()))


def cmd_verify(args) -> None:
    import requests
    import yaml

    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        suffix = f" -- {detail}" if detail else ""
        log(f"  {'PASS' if ok else 'FAIL'}  {name}{suffix}")
        if not ok:
            failures.append(name)

    log("Preflight")
    check("girder reachable", stack_up(), API_ROOT)
    check("minio reachable", minio_up(), MINIO_HOST_URL)
    if failures:
        die("Preflight failed.")

    gc = girder_client()
    collection = next(
        (c for c in gc.listCollection() if c["name"] == COLLECTION_NAME), None
    )
    if collection is None:
        die(f"Collection {COLLECTION_NAME!r} not found. Run `seed.py seed`.")

    roots = {
        f["name"]: f["_id"] for f in gc.listFolder(collection["_id"], "collection")
    }

    log("\nHierarchy")
    check("trial folder exists", "trial" in roots)
    check("ultrasound folder exists", "ultrasound" in roots)

    patients = list(gc.listFolder(roots["trial"], parentFolderType="folder"))
    check(
        f"{N_PATIENTS} patient folders",
        len(patients) == N_PATIENTS,
        f"got {len(patients)}",
    )

    for patient in patients:
        studies = list(gc.listFolder(patient["_id"], parentFolderType="folder"))
        check(
            f"{patient['name']}: {N_STUDIES} studies",
            len(studies) == N_STUDIES,
            f"got {len(studies)}",
        )
        for study in studies:
            series = {
                s["name"]
                for s in gc.listFolder(study["_id"], parentFolderType="folder")
            }
            check(
                f"{patient['name']}/{study['name']}: CT+PET",
                {"CT", "PET"} <= series,
                f"got {sorted(series)}",
            )

    log("\nGrouping metadata")
    sample_study = gc.listFolder(patients[0]["_id"], parentFolderType="folder")
    sample_study = list(sample_study)[0]
    ct_folder = next(
        f for f in gc.listFolder(sample_study["_id"], parentFolderType="folder")
        if f["name"] == "CT"
    )
    items = list(gc.listItem(ct_folder["_id"]))
    check("CT folder has items", bool(items), f"{len(items)} items")
    if items:
        meta = gc.getItem(items[0]["_id"]).get("meta", {}).get("dicom", {})
        for tag in ("PatientID", "StudyInstanceUID", "SeriesInstanceUID"):
            check(f"meta.dicom.{tag}", bool(meta.get(tag)), str(meta.get(tag))[:40])

    log("\nConfig items")
    for prefix in ("trial", "ultrasound"):
        names = {i["name"] for i in gc.listItem(roots[prefix])}
        check(f"{prefix}/.large_image_config.yaml", ".large_image_config.yaml" in names)
        check(f"{prefix}/{VOLVIEW_CONFIG_NAME}", VOLVIEW_CONFIG_NAME in names)

    # Checking that the item exists is not enough: the config resolves by item
    # name, so a name mismatch leaves the endpoint quietly serving BASE_CONFIG.
    # Compare what the server actually returns against what we uploaded.
    log("\nServed VolView config")
    for prefix in ("trial", "ultrasound"):
        local = yaml.safe_load((CONFIGS_DIR / prefix / VOLVIEW_CONFIG_NAME).read_text())

        bad_types = set(local.get("disabledViewTypes", [])) - VALID_DISABLED_VIEW_TYPES
        check(
            f"{prefix}: disabledViewTypes are valid",
            not bad_types,
            f"invalid {bad_types}",
        )
        bad_views = collect_layout_views(local.get("layouts", {})) - VALID_LAYOUT_VIEWS
        check(
            f"{prefix}: layout view names are valid",
            not bad_views,
            f"invalid {bad_views}",
        )

        served = gc.get(
            f"folder/{roots[prefix]}/volview_config/{VOLVIEW_CONFIG_NAME}"
        )
        check(
            f"{prefix}: disabledViewTypes applied",
            served.get("disabledViewTypes") == local["disabledViewTypes"],
            f"served {served.get('disabledViewTypes')}",
        )
        authored = set(local["layouts"]) - {"__all__"}
        check(
            f"{prefix}: layouts applied",
            authored <= set(served.get("layouts", {})),
            f"served {sorted(served.get('layouts', {}))}",
        )
        # First key of the served map is the one VolView switches to.
        active = next(iter(served.get("layouts", {})), None)
        check(
            f"{prefix}: opens in {EXPECTED_ACTIVE_LAYOUT[prefix]!r}",
            active == EXPECTED_ACTIVE_LAYOUT[prefix],
            f"would open {active!r}",
        )

    log("\nVolView manifest")
    token = gc.token
    headers = {"Girder-Token": token}
    manifest = requests.get(
        f"{API_ROOT}/folder/{ct_folder['_id']}/volview", headers=headers, timeout=30
    )
    check(
        "folder/:id/volview responds",
        manifest.status_code == 200,
        str(manifest.status_code),
    )
    if manifest.status_code == 200:
        resources = manifest.json().get("resources", [])
        check("manifest has resources", bool(resources), f"{len(resources)} entries")
        proxiable = [r for r in resources if "/proxiable/" in r.get("url", "")]
        check("urls are proxiable", bool(proxiable), f"{len(proxiable)} proxiable")
        if proxiable:
            url = proxiable[0]["url"]
            if url.startswith("/"):
                url = GIRDER_URL.rstrip("/") + url
            resp = requests.get(
                url, headers=headers, allow_redirects=False, stream=True, timeout=30
            )
            # A 303 here means Girder handed the browser a presigned minio:9000
            # URL, which the host cannot resolve. Streaming (200) is what we want.
            check(
                "file streams through girder (not a redirect)",
                resp.status_code == 200,
                f"status {resp.status_code}",
            )

    log("\nUltrasound clips")
    us_items = list(gc.listItem(roots["ultrasound"]))
    clips = [i for i in us_items if i["name"].endswith(".dcm")]
    check(f"{N_CLIPS} clips", len(clips) == N_CLIPS, f"got {len(clips)}")
    for clip in clips:
        meta = gc.getItem(clip["_id"]).get("meta", {})
        frames = meta.get("dicom", {}).get("NumberOfFrames")
        check(
            f"{clip['name']} is cine",
            bool(frames) and int(frames) > 1,
            f"frames={frames}",
        )

    if failures:
        die(f"{len(failures)} checks failed: {', '.join(failures)}")
    log(f"\nAll checks passed. Open {GIRDER_URL}/#collection/{collection['_id']}")


def cmd_reset(args) -> None:
    gc = girder_client()

    collection = next(
        (c for c in gc.listCollection() if c["name"] == COLLECTION_NAME), None
    )
    if collection:
        log(f"Deleting collection {COLLECTION_NAME!r}")
        gc.delete(f"collection/{collection['_id']}")
    else:
        log(f"No collection {COLLECTION_NAME!r} to delete")

    store = next(
        (s for s in gc.get("assetstore") if s["name"] == ASSETSTORE_NAME), None
    )
    if store:
        log(f"Deleting assetstore {ASSETSTORE_NAME!r}")
        try:
            gc.delete(f"assetstore/{store['_id']}")
        except Exception as exc:
            log(f"  could not delete assetstore: {exc}")

    if args.bucket:
        log(f"Emptying bucket {BUCKET}")
        s3 = s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
        STAGED_PATH.unlink(missing_ok=True)

    log("Reset complete.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("select", help="Query IDC and pin series into manifest/series.json")

    fetch = sub.add_parser("fetch", help="Download pinned series and verify them")
    fetch.add_argument("--force", action="store_true", help="Re-download everything")
    fetch.add_argument(
        "--small",
        action="store_true",
        help="Only the small-tier series the e2e compat suite uploads",
    )

    stage = sub.add_parser("stage", help="Lay out the bucket and push to MinIO")
    stage.add_argument(
        "--max-slices",
        type=int,
        default=40,
        help="Slices per CT/PET series (S3 import makes one item per slice)",
    )
    stage.add_argument(
        "--force", action="store_true", help="Re-upload existing objects"
    )

    sub.add_parser(
        "seed", help="Create assetstore, import, set metadata, upload configs"
    )

    seed_small = sub.add_parser(
        "seed-small",
        help="Plain-upload the small-tier DICOM slices into a folder (no MinIO)",
    )
    seed_small.add_argument(
        "--folder-id", required=True, help="Girder folder to upload into"
    )
    seed_small.add_argument(
        "--slices", type=int, default=3, help="Slices per series (uniform stride)"
    )

    sub.add_parser("verify", help="Assert the seeded hierarchy actually works")

    reset = sub.add_parser("reset", help="Delete the Girder collection and assetstore")
    reset.add_argument(
        "--bucket", action="store_true", help="Also empty the MinIO bucket"
    )

    args = parser.parse_args()
    {
        "select": cmd_select,
        "fetch": cmd_fetch,
        "stage": cmd_stage,
        "seed": cmd_seed,
        "seed-small": cmd_seed_small,
        "verify": cmd_verify,
        "reset": cmd_reset,
    }[args.command](args)


if __name__ == "__main__":
    main()
