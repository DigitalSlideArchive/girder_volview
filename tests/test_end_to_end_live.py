"""End-to-end live-stack tests: submit a real job and prove the result comes back.

Unlike the pytest-girder route tests (``test_job_output_binding_routes`` et al.),
which drive an *in-process* server and **synthesize** the ``data.process`` upload
event, these run against the **real running dsa stack** -- girder on :8080 +
``girder_worker`` + the registered ``volview-radiology-cli`` docker image. They
therefore exercise the one leg no offline test can reach: ``girder_worker``
uploading each output under its OWN per-hook token, and the facade correlating
that upload back to the job. That correlation (the token-mismatch fix,
facade ``c499b78``) is the whole payoff -- a succeeded job whose results actually
appear -- and on the pre-fix code this exact flow left ``volviewOutputs`` ``{}``
and ``/results`` a silent ``[]``.

Self-skipping on reachability -- the same pattern ``test_job_output_binding_routes``
uses for its test Mongo: they run automatically when the dsa stack answers at
``GIRDER_URL`` and skip when it does not, so the offline gate (``tox -e test``)
stays green with the stack down and gains real coverage when it is up. No env
var to remember -- just bring the stack up (+ ``./ensure-radiology-cli.sh``)::

    python -m pytest tests/test_end_to_end_live.py -v

Env overrides: ``GIRDER_URL`` (default ``http://localhost:8080``),
``DSA_ADMIN_USER`` (``admin``), ``DSA_ADMIN_PASS`` (``password``).
"""

import os
import time
import urllib.request
import uuid

import pytest


# ---------------------------------------------------------------------------
# Reachability self-skip (mirrors test_job_output_binding_routes, but gates on
# the *real* stack at GIRDER_URL, not the pytest-girder test Mongo)
# ---------------------------------------------------------------------------

GIRDER_URL = os.environ.get("GIRDER_URL", "http://localhost:8080")
API_ROOT = GIRDER_URL.rstrip("/") + "/api/v1"
ADMIN_USER = os.environ.get("DSA_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("DSA_ADMIN_PASS", "password")
OTSU_TITLE_PREFIX = "Otsu"
THRESHOLD_TITLE_PREFIX = "Threshold"

# A same-origin browser signal satisfies the facade's CSRF guard (Ch3) on the
# write routes (runTask); a server-to-server client sends none, so we add the
# exact header a same-origin fetch would.
_SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _stack_up(timeout=2.0):
    """Whether the dsa girder actually answers at API_ROOT -- an HTTP probe of
    ``/system/version``, not a bare socket connect, so a stray listener on the
    port can't false-trigger the whole module into running."""
    try:
        with urllib.request.urlopen(API_ROOT + "/system/version", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _stack_up(),
    reason=(
        "live e2e self-skips unless the dsa stack answers at GIRDER_URL/api/v1 "
        "(default http://localhost:8080); bring it up + ./ensure-radiology-cli.sh"
    ),
)


# ---------------------------------------------------------------------------
# Synthetic input volume -- three intensity tiers so multi-Otsu yields >1 label
# ---------------------------------------------------------------------------

def _structured_volume(nx, ny, nz):
    import numpy as np

    vol = np.zeros((nz, ny, nx), dtype=np.int16)
    vol[nz // 4:3 * nz // 4, ny // 4:3 * ny // 4, nx // 4:3 * nx // 4] = 400
    zz, yy, xx = np.ogrid[:nz, :ny, :nx]
    r = min(nx, ny, nz) / 4.0
    ball = (zz - nz / 2) ** 2 + (yy - ny / 2) ** 2 + (xx - nx / 2) ** 2 <= r * r
    vol[ball] = 1200
    return vol


def _write_nrrd(path, nx=24, ny=24, nz=20):
    """Hand-write a little-endian raw NRRD (ITK reads it via itk.imread)."""
    vol = _structured_volume(nx, ny, nz)
    header = (
        "NRRD0004\n"
        "type: short\n"
        "dimension: 3\n"
        "sizes: %d %d %d\n"
        "encoding: raw\n"
        "endian: little\n"
        "space dimension: 3\n"
        "space directions: (1,0,0) (0,1,0) (0,0,1)\n"
        "space origin: (0,0,0)\n"
        "\n"
    ) % (nx, ny, nz)
    with open(path, "wb") as fh:
        fh.write(header.encode("ascii"))
        fh.write(vol.astype("<i2").tobytes(order="C"))
    return path


def _write_dicom_series(dest_dir, slices=12, rows=48, cols=48):
    """Write a multi-file CT DICOM series: N real Part-10 slices sharing one
    SeriesInstanceUID with stepping ImagePositionPatient.

    The CLI's GDCM series read (assemble._read_dicom_series) groups by
    SeriesInstanceUID and orders by slice position, so the N slices assemble into
    one 3D volume -- the whole point of the multi-file test.
    """
    import numpy as np
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    os.makedirs(dest_dir, exist_ok=True)
    vol = _structured_volume(cols, rows, slices).astype(np.uint16)
    series_uid, study_uid, frame_uid = generate_uid(), generate_uid(), generate_uid()
    thickness = 2.0
    paths = []
    for k in range(slices):
        sop_uid = generate_uid()
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = Dataset()
        ds.file_meta = meta
        ds.preamble = b"\0" * 128
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"
        ds.PatientName = "E2E^Phantom"
        ds.PatientID = "E2E-000"
        ds.SeriesNumber = 1
        ds.InstanceNumber = k + 1
        ds.Rows, ds.Columns = rows, cols
        ds.PixelSpacing = [1.0, 1.0]
        ds.SliceThickness = thickness
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.ImagePositionPatient = [0.0, 0.0, float(k) * thickness]
        ds.SliceLocation = float(k) * thickness
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleIntercept = 0
        ds.RescaleSlope = 1
        ds.PixelData = vol[k].tobytes()
        path = os.path.join(dest_dir, "slice_%03d.dcm" % (k + 1))
        # Encoding comes from file_meta.TransferSyntaxUID; enforce a valid
        # Part-10 file (preamble + meta) so GDCM in the CLI reads the series.
        pydicom.dcmwrite(path, ds, enforce_file_format=True)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Girder / facade REST helpers
# ---------------------------------------------------------------------------

def _proxiable_uri(file_doc):
    # The facade's own mint (utils.makeFileDownloadUrl): origin-relative
    # /api/v1/file/<id>/proxiable/<name>. resolveInputUrisToFileIds recovers the
    # id from exactly this shape and re-checks READ ACL.
    return "/api/v1/file/%s/proxiable/%s" % (file_doc["_id"], file_doc["name"])


def _upload_item(gc, folder_id, item_name, file_paths):
    """Upload N files into ONE item; return (item, [file docs])."""
    item = gc.createItem(folder_id, item_name)
    for path in file_paths:
        gc.uploadFileToItem(item["_id"], path)
    return item, list(gc.listFile(item["_id"]))


def _find_task(gc, folder_id, prefix):
    tasks = gc.get("folder/%s/volview_processing/tasks" % folder_id)
    for task in tasks:
        if (task.get("title") or "").startswith(prefix):
            return task
    pytest.skip(
        "%s task not registered (run ./ensure-radiology-cli.sh); tasks=%s"
        % (prefix, [t.get("title") for t in tasks])
    )


def _find_otsu(gc, folder_id):
    return _find_task(gc, folder_id, OTSU_TITLE_PREFIX)


def _run_task(gc, folder_id, task_id, values):
    return gc.post(
        "folder/%s/volview_processing/tasks/%s/run" % (folder_id, task_id),
        json={"values": values},
        headers=_SAME_ORIGIN,
    )


def _poll_terminal(gc, job_id, timeout=240):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = gc.get("volview_processing/jobs/%s" % job_id)
        if last.get("state") in ("success", "error", "cancelled"):
            return last
        time.sleep(2)
    return last


def _seg_nrrd_header(gc, file_id, tmp_path):
    """Download a `.seg.nrrd` output and return its plaintext NRRD header.

    NRRD keeps its header (including custom `key:=value` fields) in plaintext
    before the compressed data section, so the embedded segment metadata is
    readable without itk — the same header itk-wasm surfaces to the client.
    """
    dest = str(tmp_path / ("out-%s.seg.nrrd" % file_id))
    gc.downloadFile(file_id, dest)
    with open(dest, "rb") as fh:
        raw = fh.read()
    header = raw.split(b"\n\n", 1)[0]
    return header.decode("ascii", "replace")


def _assert_labelmap_result(gc, job_id, final, tmp_path):
    """Shared assertions: outputs bound + an add-segment-group result resolves,
    with the segment names/colors embedded in the `.seg.nrrd` (Chunk 34)."""
    if final.get("state") != "success":
        job = gc.get("job/%s" % job_id)
        tail = "".join((job.get("log") or [])[-25:])
        pytest.fail(
            "job %s state=%s\nerrorTail=%s\nlog tail:\n%s"
            % (job_id, final.get("state"), final.get("errorTail"), tail)
        )

    # The correlation payoff: the worker's per-hook upload bound to THIS job.
    job = gc.get("job/%s" % job_id)
    outputs = job.get("volviewOutputs") or {}
    assert outputs, "volviewOutputs empty -- output->job correlation broke (silent [])"
    assert "outputLabelmap" in outputs

    # Chunk 28: /results is the `{intents, missing}` envelope.
    payload = gc.get("volview_processing/jobs/%s/results" % job_id)
    intents = payload["intents"] if isinstance(payload, dict) else payload
    assert intents, "/results returned no intents for a succeeded job"
    seg = next((r for r in intents if r.get("intent") == "add-segment-group"), None)
    assert seg is not None, "no add-segment-group intent in %s" % intents
    # Correlated to THIS job, and the bound file is real + ACL-served.
    assert seg.get("source", {}).get("jobId") == job_id
    fileDoc = gc.getFile(seg["id"])
    assert fileDoc and int(fileDoc.get("size") or 0) > 0
    # Chunk 34: the segment name/color travel INSIDE the .seg.nrrd (the facade
    # sets no `segments` payload) — prove it from the output file's own header.
    assert seg["name"].endswith(".seg.nrrd"), (
        "labelmap output is not a .seg.nrrd: %s" % seg.get("name")
    )
    assert "segments" not in seg, "facade should fold no sidecar (Chunk 34)"
    header = _seg_nrrd_header(gc, seg["id"], tmp_path)
    assert "Segment0_Name" in header and "Segment0_Color" in header, (
        "embedded segment metadata missing from the .seg.nrrd header:\n%s" % header
    )
    return seg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gc():
    girder_client = pytest.importorskip("girder_client")
    pytest.importorskip("numpy")
    client = girder_client.GirderClient(apiUrl=API_ROOT)
    client.authenticate(ADMIN_USER, ADMIN_PASS)
    return client


@pytest.fixture
def e2e_folder(gc):
    me = gc.get("user/me")
    folder = gc.createFolder(
        me["_id"], "volview-e2e-%s" % uuid.uuid4().hex[:8],
        parentType="user", public=False,
    )
    yield folder
    try:
        gc.delete("folder/%s" % folder["_id"])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. Single-volume segmentation -- the output->job token-correlation regression
# ---------------------------------------------------------------------------

def test_single_volume_result_correlates_to_job(gc, e2e_folder, tmp_path):
    nrrd = _write_nrrd(str(tmp_path / "phantom.nrrd"))
    _, files = _upload_item(gc, e2e_folder["_id"], "single-volume", [nrrd])
    uris = [_proxiable_uri(f) for f in files]
    assert len(uris) == 1

    task = _find_otsu(gc, e2e_folder["_id"])
    # Client-faithful body: only the bound input + a scalar param. NO output
    # entries -- the facade autofills output names (_autofillOutputs).
    values = {
        "inputVolume": {"type": "image", "uris": uris},
        "numberOfLevels": 3,
    }
    submitted = _run_task(gc, e2e_folder["_id"], task["id"], values)
    job_id = submitted["jobId"]

    final = _poll_terminal(gc, job_id)
    _assert_labelmap_result(gc, job_id, final, tmp_path)


# ---------------------------------------------------------------------------
# 1b. Crash path -- a CLI that raises must reach ERROR, not a silent success
# ---------------------------------------------------------------------------

def test_crashed_cli_reports_error_not_silent_success(gc, e2e_folder, tmp_path):
    """A CLI that raises (ThresholdSegmentation with lower > upper) must drive the
    job to ERROR with a non-empty log tail, and /results must return the explicit
    non-success 400 -- never a silent success with empty results.

    This is the dispatcher-honesty payoff (Chunk 26): ``cli_list`` now propagates
    the child's exit code, so girder_worker sees a non-zero exit and marks the job
    failed. On the pre-fix image the crashed CLI exited 0, the job reported
    ``success``, and ``/results`` was a silent ``[]``.
    """
    import girder_client

    nrrd = _write_nrrd(str(tmp_path / "phantom.nrrd"))
    _, files = _upload_item(gc, e2e_folder["_id"], "crash-input", [nrrd])
    uris = [_proxiable_uri(f) for f in files]

    task = _find_task(gc, e2e_folder["_id"], THRESHOLD_TITLE_PREFIX)
    values = {
        "inputVolume": {"type": "image", "uris": uris},
        "lowerThreshold": 200,
        "upperThreshold": 100,  # lower > upper => the CLI raises before any output
    }
    submitted = _run_task(gc, e2e_folder["_id"], task["id"], values)
    job_id = submitted["jobId"]

    final = _poll_terminal(gc, job_id)
    assert final.get("state") == "error", (
        "crashed CLI did not surface as error: state=%s -- the dispatcher swallowed "
        "the child exit code?" % final.get("state")
    )
    job = gc.get("job/%s" % job_id)
    log_tail = "".join((job.get("log") or [])[-25:])
    combined = (final.get("errorTail") or "") + log_tail
    assert combined.strip(), "error job carried no log tail"
    # Specifically the lower>upper guard fired -- not some unrelated failure --
    # so this stays a regression wall for the crash path, not just "any error".
    assert "Lower threshold" in combined, (
        "error tail did not mention the threshold guard: %r" % combined[-500:]
    )

    # /results is an explicit permanent error (400), not an empty list.
    with pytest.raises(girder_client.HttpError) as exc:
        gc.get("volview_processing/jobs/%s/results" % job_id)
    assert exc.value.status == 400


# ---------------------------------------------------------------------------
# 2. Multi-file DICOM series -> assembled volume -> labelmap
# ---------------------------------------------------------------------------

def test_multifile_dicom_series_result_correlates_to_job(gc, e2e_folder, tmp_path):
    """A real DICOM series (N slices) fed as the background yields a labelmap.

    This is the shape the browser client actually mints for a DICOM-series
    background: N proxiable uris (one per slice) with ``format:"dicom-series"``.
    The facade forwards N Girder file ids; the CLI's ``inputVolume`` carries
    ``reference="_girder_id_"`` so slicer_cli_web passes the ids through and the
    CLI fetches + GDCM-assembles them into one 3D volume before segmenting. Before
    that wiring, this submit 400'd with ``Invalid ObjectId`` (a single ``<image>``
    only accepts one id).
    """
    pytest.importorskip("pydicom")
    slices = _write_dicom_series(str(tmp_path / "series"))
    assert len(slices) > 1, "a series must be more than one file"
    _, files = _upload_item(gc, e2e_folder["_id"], "dicom-series", slices)
    uris = [_proxiable_uri(f) for f in files]
    assert len(uris) == len(slices)

    task = _find_otsu(gc, e2e_folder["_id"])
    values = {
        "inputVolume": {"type": "image", "format": "dicom-series", "uris": uris},
        "numberOfLevels": 3,
    }
    submitted = _run_task(gc, e2e_folder["_id"], task["id"], values)
    job_id = submitted["jobId"]

    final = _poll_terminal(gc, job_id)
    _assert_labelmap_result(gc, job_id, final, tmp_path)
