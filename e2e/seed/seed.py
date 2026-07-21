# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "idc-index",
#     "pydicom",
#     "boto3",
#     "girder-client",
#     "pyyaml",
#     "requests",
#     "simpleitk",
# ]
# ///
"""
Seed a local DSA/Girder with public CC-BY DICOM via a simulated S3 import.

Pipeline (each step is idempotent and re-runnable):

    uv run seed.py select     # query IDC -> manifest/series.json  (rare)
    uv run seed.py fetch      # download pinned series -> data/    + ATTRIBUTION.md
    uv run seed.py stage      # arrange into bucket layout -> MinIO
    uv run seed.py seed       # assetstore + import + study metadata + configs
    uv run seed.py reseed     # delete + recreate collections from staged data
    uv run seed.py verify     # assert the whole thing actually works
    uv run seed.py reset      # tear down the Girder side

Data is NOT vendored: `select` pins SeriesInstanceUIDs, `fetch` downloads them
from IDC's public bucket. See ATTRIBUTION.md for the terms that ride along.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).parent.resolve()
DATA_DIR = HERE / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
DEVELOPER_DATA_DIR = DATA_DIR / "developer"
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
    "patients": "Axial Coronal Sagittal",
    "prostate": "Axial Coronal Sagittal",
    "ultrasound": "1 Cine (single pane)",
}

UNFILTERED_COLLECTION_NAME = "Trial"
FILTERED_COLLECTION_NAME = "Trial (Large Image Filter)"
DEVELOPER_COLLECTION_NAME = "Developer"
COLLECTION_NAMES = (
    UNFILTERED_COLLECTION_NAME,
    FILTERED_COLLECTION_NAME,
    DEVELOPER_COLLECTION_NAME,
)
TRIAL_COLLECTION_NAMES = (
    UNFILTERED_COLLECTION_NAME,
    FILTERED_COLLECTION_NAME,
)
ASSETSTORE_NAME = "Devkit MinIO"
ASSETSTORE_TYPE_S3 = 2  # girder.constants.AssetstoreType.S3

DEVELOPER_DOWNLOADS = {
    "prostate": {
        "url": "https://data.kitware.com/api/v1/file/63527c7311dab8142820a339/download",
        "name": "MRI-PROSTATEx-0004.zip",
        "sha512": (
            "4f5c5e8a8230e950ae6dd280f3128ef62ac5d44e5e49c6c1f2d3e07482df4d7b"
            "e0bf55986f8f397b0f9671c9a9b75fc4fbf4e93fbbc889a701939f3780d3b2b3"
        ),
    },
    "prostate_seg": {
        "url": "https://data.kitware.com/api/v1/file/692f13ed80eaefe49a4abb72/download",
        "name": "prostate-total.seg.nii.gz",
        "sha512": (
            "bb2919662086e670bf4666f803a27f6ffd95cc5461e3381cb0d4e50df7e62c864"
            "9ec6a2a2bb6cd726d73542db71831280408251139d8b24dde5e28bd22886459"
        ),
    },
    "fetus": {
        "url": "https://data.kitware.com/api/v1/file/635679c311dab8142820a4f5/download",
        "name": "3DUS-Fetus.mha",
        "sha512": (
            "93342dfe499ac855a4e51ed9bf16358fe265a17ca08b03f04a0cb43538afc5fdb"
            "6633d9ef4e629de93845c7a859f91993a452152c788104fcb04652358cd3ead"
        ),
    },
}

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

# DICOM tags retained in the staging plan. girder_volview reads the instance
# tags into meta.dicom.* during import; the plan also carries study-level fields
# derived from the selected series.
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
            f"  warning: only {len(patients)} qualifying patients (wanted {N_PATIENTS})"
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


def sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(source: dict, force: bool = False) -> Path:
    """Download a pinned example and reject incomplete or changed content."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = DOWNLOAD_DIR / source["name"]
    if destination.exists() and not force and sha512(destination) == source["sha512"]:
        log(f"  {source['name']}: already present")
        return destination

    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    log(f"  downloading {source['name']}...")
    try:
        urllib.request.urlretrieve(source["url"], partial)
        actual = sha512(partial)
        if actual != source["sha512"]:
            die(
                f"SHA-512 mismatch for {source['name']}: "
                f"expected {source['sha512']}, got {actual}"
            )
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def add_segmentation_metadata(image, segments: list[tuple[int, str, str]]) -> None:
    """Add the core 3D Slicer segmentation fields understood by VolView."""
    fields = {
        "Segmentation_ContainedRepresentationNames": "Binary labelmap|",
        "Segmentation_MasterRepresentation": "Binary labelmap",
    }
    for index, (label, name, color) in enumerate(segments):
        fields.update(
            {
                f"Segment{index}_ID": name.lower().replace(" ", "_"),
                f"Segment{index}_Name": name,
                f"Segment{index}_Color": color,
                f"Segment{index}_LabelValue": str(label),
                f"Segment{index}_Layer": "0",
            }
        )
    for key, value in fields.items():
        image.SetMetaData(key, value)


def prepare_developer_examples(force: bool = False) -> None:
    """Fetch VolView's prostate/fetus examples and create associated NRRDs."""
    import SimpleITK as sitk

    log("\nFetching developer examples...")
    downloads = {
        key: download_verified(source, force)
        for key, source in DEVELOPER_DOWNLOADS.items()
    }

    prostate_dir = DEVELOPER_DATA_DIR / "prostate"
    prostate_dicom_dir = prostate_dir / "dicom"
    fetus_dir = DEVELOPER_DATA_DIR / "fetus"
    prostate_dicom_dir.mkdir(parents=True, exist_ok=True)
    fetus_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(downloads["prostate"]) as archive:
        dicom_members = [
            member for member in archive.infolist() if member.filename.endswith(".dcm")
        ]
        if not dicom_members:
            die(f"{downloads['prostate'].name} contains no DICOM instances")
        for member in dicom_members:
            destination = prostate_dicom_dir / Path(member.filename).name
            if destination.exists() and not force:
                continue
            with archive.open(member) as source, open(destination, "wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)

    prostate_seg = prostate_dir / "5.seg.total-segmentator.nrrd"
    image = sitk.ReadImage(str(downloads["prostate_seg"]))
    stats = sitk.LabelShapeStatisticsImageFilter()
    stats.Execute(image)
    labels = sorted(int(label) for label in stats.GetLabels())
    palette = ("0.9 0.2 0.2", "0.2 0.7 0.9", "0.3 0.8 0.3", "0.9 0.7 0.2")
    add_segmentation_metadata(
        image,
        [
            (label, f"Prostate label {label}", palette[index % len(palette)])
            for index, label in enumerate(labels)
        ],
    )
    sitk.WriteImage(image, str(prostate_seg), True)

    fetus_image = fetus_dir / "fetus.mha"
    if force or not fetus_image.exists():
        fetus_image.write_bytes(downloads["fetus"].read_bytes())

    fetus_seg = fetus_dir / "fetus.seg.nrrd"
    image = sitk.ReadImage(str(fetus_image))
    segmentation = sitk.Cast(sitk.OtsuThreshold(image, 0, 1), sitk.sitkUInt8)
    add_segmentation_metadata(segmentation, [(1, "Fetus foreground", "0.95 0.65 0.2")])
    sitk.WriteImage(segmentation, str(fetus_seg), True)

    log(f"  prostate: {len(list(prostate_dicom_dir.glob('*.dcm')))} DICOM slices")
    log(f"  prostate segmentation: {prostate_seg.name}")
    log(f"  fetus image + segmentation: {fetus_image.name}, {fetus_seg.name}")


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
        prepare_developer_examples(args.force)
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
- Litjens, G., Debats, O., Barentsz, J., Karssemeijer, N., & Huisman, H.
  (2017). *SPIE-AAPM PROSTATEx Challenge Data* (Version 2) [Dataset]. The
  Cancer Imaging Archive. https://doi.org/10.7937/K9TCIA.2017.MURS5CL

IDC selection DOIs: {", ".join(dois)}

## VolView developer examples

The prostate subset and fetal ultrasound volume are pinned by SHA-512 and
downloaded from Kitware's public VolView example-data folder:

- `MRI-PROSTATEx-0004.zip`
- `prostate-total.seg.nii.gz` (converted locally to `.seg.nrrd`)
- `3DUS-Fetus.mha`

The fetal segmentation is generated locally with Otsu thresholding; it is not
a clinical annotation.

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
    """Retain selected DICOM tags in the staging plan."""
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
                    "content_type": "application/dicom",
                }
            )

    developer_files = {
        "developer/prostate/5.seg.total-segmentator.nrrd": (
            DEVELOPER_DATA_DIR / "prostate" / "5.seg.total-segmentator.nrrd",
            "application/octet-stream",
        ),
        "developer/fetus/fetus.mha": (
            DEVELOPER_DATA_DIR / "fetus" / "fetus.mha",
            "application/octet-stream",
        ),
        "developer/fetus/fetus.seg.nrrd": (
            DEVELOPER_DATA_DIR / "fetus" / "fetus.seg.nrrd",
            "application/octet-stream",
        ),
    }
    prostate_dicom = sorted((DEVELOPER_DATA_DIR / "prostate" / "dicom").glob("*.dcm"))
    if not prostate_dicom:
        die("Developer examples are missing. Run `seed.py fetch`.")
    for path in prostate_dicom:
        developer_files[f"developer/prostate/dicom/{path.name}"] = (
            path,
            "application/dicom",
        )
    for key, (path, content_type) in developer_files.items():
        if not path.exists():
            die(f"Developer example {path} is missing. Run `seed.py fetch`.")
        objects.append(
            {
                "key": key,
                "local": str(path),
                "meta": {},
                "content_type": content_type,
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
            ExtraArgs={"ContentType": obj.get("content_type", "application/dicom")},
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


def import_prefix(
    gc,
    assetstore_id: str,
    prefix: str,
    folder_id: str,
    file_include_regex: str = r".*\.dcm$",
) -> None:
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
            "fileIncludeRegex": file_include_regex,
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


def add_derived_dicom_metadata(gc, item_id: str, metadata: dict) -> None:
    """Merge seed-derived fields with girder_volview's parsed DICOM metadata."""
    item = gc.getItem(item_id)
    dicom = item.get("meta", {}).get("dicom", {})
    gc.addMetadataToItem(item_id, {"dicom": {**dicom, **metadata}})


def apply_study_metadata(gc, plan: dict, roots: dict) -> int:
    """Add study-level fields that are absent from individual DICOM instances.

    girder_volview populates the instance tags synchronously from
    ``model.file.save.after`` for both uploads and asset-store imports.
    ``ModalitiesInStudy`` is derived from all series staged for a study, so the
    seed adds only that enrichment without replacing the parsed tags.
    """
    log("Applying derived study metadata to imported items...")

    # folder id -> {item name: item id}, filled lazily.
    item_cache: dict[str, dict] = {}
    path_cache: dict[str, dict] = {}
    applied, orphaned = 0, []

    for obj in plan["objects"]:
        derived = {
            key: obj["meta"][key]
            for key in ("ModalitiesInStudy",)
            if key in obj["meta"]
        }
        if not derived:
            continue

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

        add_derived_dicom_metadata(gc, item_id, derived)
        applied += 1
        if applied % 50 == 0:
            log(f"  {applied}...")

    if orphaned:
        log(f"  warning: {len(orphaned)} staged objects had no matching item")
        for key in orphaned[:5]:
            log(f"    {key}")
    log(f"  applied derived metadata to {applied} items")
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

    collections = {}
    try:
        assetstore = ensure_assetstore(gc)

        for collection_name in TRIAL_COLLECTION_NAMES:
            collection = ensure_collection(gc, collection_name)
            collections[collection_name] = collection
            log(f"Collection {collection_name!r}: {collection['_id']}")
            patients = ensure_folder(gc, collection["_id"], "collection", "patients")
            ultrasound = ensure_folder(
                gc, collection["_id"], "collection", "ultrasound"
            )
            import_prefix(gc, assetstore["_id"], "trial/", patients["_id"])
            import_prefix(gc, assetstore["_id"], "ultrasound/", ultrasound["_id"])
            apply_study_metadata(gc, plan, {"trial": patients["_id"]})

            trial_config_dir = CONFIGS_DIR / "trial"
            upload_config(gc, patients["_id"], trial_config_dir / VOLVIEW_CONFIG_NAME)
            log(f"  {collection_name}/patients/{VOLVIEW_CONFIG_NAME}")
            if collection_name == FILTERED_COLLECTION_NAME:
                large_image_config = trial_config_dir / ".large_image_config.yaml"
                upload_config(gc, patients["_id"], large_image_config)
                log(f"  {collection_name}/patients/{large_image_config.name}")
            ultrasound_config = CONFIGS_DIR / "ultrasound" / VOLVIEW_CONFIG_NAME
            upload_config(gc, ultrasound["_id"], ultrasound_config)
            log(f"  {collection_name}/ultrasound/{VOLVIEW_CONFIG_NAME}")
            if collection_name == FILTERED_COLLECTION_NAME:
                ultrasound_filter = (
                    CONFIGS_DIR / "ultrasound" / ".large_image_config.yaml"
                )
                upload_config(gc, ultrasound["_id"], ultrasound_filter)
                log(f"  {collection_name}/ultrasound/{ultrasound_filter.name}")

        developer = ensure_collection(gc, DEVELOPER_COLLECTION_NAME)
        collections[DEVELOPER_COLLECTION_NAME] = developer
        log(f"Collection {DEVELOPER_COLLECTION_NAME!r}: {developer['_id']}")
        developer_roots = {
            name: ensure_folder(gc, developer["_id"], "collection", name)["_id"]
            for name in ("prostate", "fetus", "ultrasound")
        }
        image_regex = r".*\.(dcm|mha|nrrd)$"
        import_prefix(
            gc,
            assetstore["_id"],
            "developer/prostate/",
            developer_roots["prostate"],
            image_regex,
        )
        import_prefix(
            gc,
            assetstore["_id"],
            "developer/fetus/",
            developer_roots["fetus"],
            image_regex,
        )
        import_prefix(
            gc,
            assetstore["_id"],
            "ultrasound/",
            developer_roots["ultrasound"],
        )
        developer_config = CONFIGS_DIR / "developer" / VOLVIEW_CONFIG_NAME
        for name in ("prostate", "fetus"):
            upload_config(gc, developer_roots[name], developer_config)
            log(f"  {DEVELOPER_COLLECTION_NAME}/{name}/{VOLVIEW_CONFIG_NAME}")
        ultrasound_config = CONFIGS_DIR / "ultrasound" / VOLVIEW_CONFIG_NAME
        upload_config(gc, developer_roots["ultrasound"], ultrasound_config)
        log(f"  {DEVELOPER_COLLECTION_NAME}/ultrasound/{VOLVIEW_CONFIG_NAME}")
    finally:
        set_setting(gc, "large_image.auto_set", previous_auto_set)
        log(f"Restored large_image.auto_set to {previous_auto_set!r}")

    log("\nSeeded collections:")
    for name, collection in collections.items():
        log(f"  {name}: {GIRDER_URL}/#collection/{collection['_id']}")


def cmd_seed_small(args) -> None:
    """Upload the small-tier slices straight into a folder (no MinIO/S3).

    The e2e compat provisioning calls this against its own run folder: a couple
    of multi-file series (patient-01 study-01 CT+PET for layering, patient-02
    study-01 CT for filtering) at --slices per series and flat item names.
    girder_volview populates meta.dicom.* when each file is saved; this command
    adds the study-level modality list derived from the selected series.
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
                file_doc = gc.uploadFileToFolder(
                    args.folder_id,
                    str(path),
                    filename=name,
                    mimeType="application/dicom",
                )
                add_derived_dicom_metadata(
                    gc,
                    file_doc["itemId"],
                    {"ModalitiesInStudy": modalities},
                )
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


def find_collection(gc, name: str) -> dict | None:
    return next((c for c in gc.listCollection() if c["name"] == name), None)


def child_folders(gc, parent_id: str) -> dict[str, dict]:
    return {
        folder["name"]: folder
        for folder in gc.listFolder(parent_id, parentFolderType="folder")
    }


def imaging_tree_signature(gc, root_id: str) -> set[tuple[str, ...]]:
    """Return relative folder and non-config item paths below a folder."""
    paths: set[tuple[str, ...]] = set()

    def walk(folder_id: str, prefix: tuple[str, ...]) -> None:
        for item in gc.listItem(folder_id):
            if not item["name"].startswith("."):
                paths.add(prefix + (item["name"],))
        for name, folder in child_folders(gc, folder_id).items():
            paths.add(prefix + (name,))
            walk(folder["_id"], prefix + (name,))

    walk(root_id, ())
    return paths


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
    collections = {name: find_collection(gc, name) for name in COLLECTION_NAMES}
    for name, collection in collections.items():
        check(f"collection {name!r} exists", collection is not None)
    if any(collection is None for collection in collections.values()):
        die("Required collections are missing. Run `seed.py seed`.")

    trial_roots = {}
    ultrasound_roots = {}
    sample_ct_folder = None
    expected_patients = {f"patient-{index:02d}" for index in range(1, N_PATIENTS + 1)}
    log("\nTrial hierarchies")
    for collection_name in TRIAL_COLLECTION_NAMES:
        collection = collections[collection_name]
        roots = {
            folder["name"]: folder
            for folder in gc.listFolder(collection["_id"], "collection")
        }
        check(f"{collection_name}: patients root", "patients" in roots)
        check(f"{collection_name}: ultrasound root", "ultrasound" in roots)
        if "ultrasound" in roots:
            ultrasound_roots[collection_name] = roots["ultrasound"]
        if "patients" not in roots:
            continue
        patients_root = roots["patients"]
        trial_roots[collection_name] = patients_root
        patients = child_folders(gc, patients_root["_id"])
        check(
            f"{collection_name}: {N_PATIENTS} patients",
            set(patients) == expected_patients,
            f"got {sorted(patients)}",
        )
        for patient_name in sorted(expected_patients & patients.keys()):
            studies = child_folders(gc, patients[patient_name]["_id"])
            check(
                f"{collection_name}/{patient_name}: {N_STUDIES} studies",
                len(studies) == N_STUDIES,
                f"got {len(studies)}",
            )
            for study_name, study in studies.items():
                series = child_folders(gc, study["_id"])
                check(
                    f"{collection_name}/{patient_name}/{study_name}: CT+PET",
                    {"CT", "PET"} <= set(series),
                    f"got {sorted(series)}",
                )
                if sample_ct_folder is None and "CT" in series:
                    sample_ct_folder = series["CT"]

    if len(trial_roots) == len(TRIAL_COLLECTION_NAMES):
        signatures = {
            name: imaging_tree_signature(gc, root["_id"])
            for name, root in trial_roots.items()
        }
        check(
            "trial collections mirror one another",
            signatures[UNFILTERED_COLLECTION_NAME]
            == signatures[FILTERED_COLLECTION_NAME],
            f"{len(signatures[UNFILTERED_COLLECTION_NAME])} vs "
            f"{len(signatures[FILTERED_COLLECTION_NAME])} paths",
        )
    if len(ultrasound_roots) == len(TRIAL_COLLECTION_NAMES):
        signatures = {
            name: imaging_tree_signature(gc, root["_id"])
            for name, root in ultrasound_roots.items()
        }
        check(
            "ultrasound folders mirror one another",
            signatures[UNFILTERED_COLLECTION_NAME]
            == signatures[FILTERED_COLLECTION_NAME],
        )

    log("\nTrial configuration")
    for collection_name, root in trial_roots.items():
        names = {item["name"] for item in gc.listItem(root["_id"])}
        should_filter = collection_name == FILTERED_COLLECTION_NAME
        expected_state = "present" if should_filter else "absent"
        check(
            f"{collection_name}: large-image filter {expected_state}",
            (".large_image_config.yaml" in names) == should_filter,
        )
        check(
            f"{collection_name}: VolView config present",
            VOLVIEW_CONFIG_NAME in names,
        )
        ultrasound = ultrasound_roots.get(collection_name)
        if ultrasound:
            ultrasound_names = {item["name"] for item in gc.listItem(ultrasound["_id"])}
            check(
                f"{collection_name}/ultrasound: large-image filter {expected_state}",
                (".large_image_config.yaml" in ultrasound_names) == should_filter,
            )
            check(
                f"{collection_name}/ultrasound: VolView config present",
                VOLVIEW_CONFIG_NAME in ultrasound_names,
            )

    if sample_ct_folder is not None:
        items = list(gc.listItem(sample_ct_folder["_id"]))
        check("CT series has items", bool(items), f"{len(items)} items")
        if items:
            meta = gc.getItem(items[0]["_id"]).get("meta", {}).get("dicom", {})
            for tag in ("PatientID", "StudyInstanceUID", "SeriesInstanceUID"):
                check(f"meta.dicom.{tag}", bool(meta.get(tag)), str(meta.get(tag))[:40])

    log("\nDeveloper examples")
    developer = collections[DEVELOPER_COLLECTION_NAME]
    developer_roots = {
        folder["name"]: folder
        for folder in gc.listFolder(developer["_id"], "collection")
    }
    check(
        "developer sibling folders",
        set(developer_roots) == {"prostate", "fetus", "ultrasound"},
        f"got {sorted(developer_roots)}",
    )
    prostate = developer_roots.get("prostate")
    fetus = developer_roots.get("fetus")
    developer_ultrasound = developer_roots.get("ultrasound")
    if developer_ultrasound:
        names = {item["name"] for item in gc.listItem(developer_ultrasound["_id"])}
        check(
            "Developer/ultrasound: large-image filter absent",
            ".large_image_config.yaml" not in names,
        )
        check(
            "Developer/ultrasound: VolView config present",
            VOLVIEW_CONFIG_NAME in names,
        )
    if prostate:
        prostate_items = {item["name"]: item for item in gc.listItem(prostate["_id"])}
        prostate_folders = child_folders(gc, prostate["_id"])
        check("prostate DICOM folder", "dicom" in prostate_folders)
        check(
            "prostate segmentation",
            "5.seg.total-segmentator.nrrd" in prostate_items,
        )
        if "dicom" in prostate_folders:
            dicom_items = list(gc.listItem(prostate_folders["dicom"]["_id"]))
            check(
                "prostate has real DICOM",
                bool(dicom_items),
                f"{len(dicom_items)} slices",
            )
            seg_item = prostate_items.get("5.seg.total-segmentator.nrrd")
            if seg_item:
                checked = gc.get(
                    f"folder/{prostate['_id']}/volview",
                    parameters={
                        "folders": prostate_folders["dicom"]["_id"],
                        "items": seg_item["_id"],
                    },
                )
                checked_names = {
                    entry["name"] for entry in checked.get("resources", [])
                }
                check(
                    "prostate Open Checked manifest",
                    "5.seg.total-segmentator.nrrd" in checked_names
                    and any(name.endswith(".dcm") for name in checked_names),
                    f"got {len(checked_names)} resources",
                )
    if fetus:
        fetus_items = {item["name"]: item for item in gc.listItem(fetus["_id"])}
        check("fetus image", "fetus.mha" in fetus_items)
        check("fetus segmentation", "fetus.seg.nrrd" in fetus_items)
        selected = [
            fetus_items[name]["_id"]
            for name in ("fetus.mha", "fetus.seg.nrrd")
            if name in fetus_items
        ]
        if len(selected) == 2:
            checked = gc.get(
                f"folder/{fetus['_id']}/volview",
                parameters={"items": ",".join(selected)},
            )
            checked_names = {entry["name"] for entry in checked.get("resources", [])}
            check(
                "fetus Open Checked manifest",
                {"fetus.mha", "fetus.seg.nrrd"} <= checked_names,
                f"got {sorted(checked_names)}",
            )

    log("\nServed VolView config")
    ultrasound = ultrasound_roots.get(UNFILTERED_COLLECTION_NAME)
    config_targets = []
    if FILTERED_COLLECTION_NAME in trial_roots:
        config_targets.append(
            (
                "patients",
                trial_roots[FILTERED_COLLECTION_NAME]["_id"],
                CONFIGS_DIR / "trial",
            )
        )
    if prostate:
        config_targets.append(("prostate", prostate["_id"], CONFIGS_DIR / "developer"))
    if ultrasound:
        config_targets.append(
            ("ultrasound", ultrasound["_id"], CONFIGS_DIR / "ultrasound")
        )
    for name, folder_id, config_dir in config_targets:
        local = yaml.safe_load((config_dir / VOLVIEW_CONFIG_NAME).read_text())
        bad_types = set(local.get("disabledViewTypes", [])) - VALID_DISABLED_VIEW_TYPES
        check(f"{name}: disabledViewTypes valid", not bad_types, f"invalid {bad_types}")
        bad_views = collect_layout_views(local.get("layouts", {})) - VALID_LAYOUT_VIEWS
        check(f"{name}: layout views valid", not bad_views, f"invalid {bad_views}")
        served = gc.get(f"folder/{folder_id}/volview_config/{VOLVIEW_CONFIG_NAME}")
        check(
            f"{name}: disabledViewTypes applied",
            served.get("disabledViewTypes") == local.get("disabledViewTypes"),
            f"served {served.get('disabledViewTypes')}",
        )
        authored = set(local.get("layouts", {})) - {"__all__"}
        check(
            f"{name}: layouts applied",
            authored <= set(served.get("layouts", {})),
            f"served {sorted(served.get('layouts', {}))}",
        )
        active = next(iter(served.get("layouts", {})), None)
        check(
            f"{name}: opens in {EXPECTED_ACTIVE_LAYOUT[name]!r}",
            active == EXPECTED_ACTIVE_LAYOUT[name],
            f"would open {active!r}",
        )

    log("\nVolView manifest")
    token = gc.token
    headers = {"Girder-Token": token}
    manifest = (
        requests.get(
            f"{API_ROOT}/folder/{sample_ct_folder['_id']}/volview",
            headers=headers,
            timeout=30,
        )
        if sample_ct_folder
        else None
    )
    check(
        "folder/:id/volview responds",
        manifest is not None and manifest.status_code == 200,
        str(manifest.status_code if manifest else "no CT folder"),
    )
    if manifest is not None and manifest.status_code == 200:
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

    if ultrasound:
        log("\nUltrasound clips")
        all_ultrasound_roots = {
            **ultrasound_roots,
            **(
                {DEVELOPER_COLLECTION_NAME: developer_ultrasound}
                if developer_ultrasound
                else {}
            ),
        }
        for collection_name, root in all_ultrasound_roots.items():
            clips = [
                item
                for item in gc.listItem(root["_id"])
                if item["name"].endswith(".dcm")
            ]
            check(
                f"{collection_name}: {N_CLIPS} clips",
                len(clips) == N_CLIPS,
                f"got {len(clips)}",
            )
            for clip in clips:
                meta = gc.getItem(clip["_id"]).get("meta", {})
                frames = meta.get("dicom", {}).get("NumberOfFrames")
                check(
                    f"{collection_name}/{clip['name']} is cine",
                    bool(frames) and int(frames) > 1,
                    f"frames={frames}",
                )

    if failures:
        die(f"{len(failures)} checks failed: {', '.join(failures)}")
    log("\nAll checks passed.")


def cmd_reset(args) -> None:
    gc = girder_client()

    for collection_name in COLLECTION_NAMES:
        collection = find_collection(gc, collection_name)
        if collection:
            log(f"Deleting collection {collection_name!r}")
            gc.delete(f"collection/{collection['_id']}")
        else:
            log(f"No collection {collection_name!r} to delete")

    if getattr(args, "bucket", False):
        log(f"Emptying bucket {BUCKET}")
        s3 = s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
        STAGED_PATH.unlink(missing_ok=True)

    log("Reset complete.")


def cmd_reseed(args) -> None:
    """Replace the managed Girder collections using the staged MinIO objects."""
    cmd_reset(argparse.Namespace(bucket=False))
    cmd_seed(args)


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
    sub.add_parser(
        "reseed", help="Delete and recreate all three collections from staged data"
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

    reset = sub.add_parser("reset", help="Delete the seeded Girder collections")
    reset.add_argument(
        "--bucket", action="store_true", help="Also empty the MinIO bucket"
    )

    args = parser.parse_args()
    {
        "select": cmd_select,
        "fetch": cmd_fetch,
        "stage": cmd_stage,
        "seed": cmd_seed,
        "reseed": cmd_reseed,
        "seed-small": cmd_seed_small,
        "verify": cmd_verify,
        "reset": cmd_reset,
    }[args.command](args)


if __name__ == "__main__":
    main()
