"""Coverage for volume staging at submit (D10 part 2, item 3.3 — b1 assemble).

A multi-file DICOM series sourceRef (item 3.2) names a whole volume; every CLI
declares an ``<image>``/``<file>`` input that wants one file, so at submit the
facade resolves the series to its ordered slices and assembles them into one
geometry-correct NRRD with SimpleITK, binds that file id, and tracks the
transient for deletion when the job finishes.

The geometry-guard test exercises the real SimpleITK assembly (the ``[1,1,1]``
regression guard, DICOM_SPACING_FIX_PLAN.md). The dispatch / cleanup tests drive
the pure control flow with fake Girder models, the same spirit as
``test_loaded_sources``/``test_processing_source_ref`` (no live Girder).
"""

import os
import tempfile

import numpy as np
import pytest
import SimpleITK as sitk
from bson.objectid import ObjectId

from girder.exceptions import RestException
from girder_jobs.constants import JobStatus

from girder_volview.facade import processing


# ---------------------------------------------------------------------------
# Geometry guard — real SimpleITK multi-slice assembly preserves geometry
# ---------------------------------------------------------------------------

def _write_synthetic_series(d, spacing, origin, n=5):
    """Write ``n`` single-slice DICOM files with known geometry; return paths.

    Geometry travels in per-slice ``ImagePositionPatient`` (0020|0032) /
    ``PixelSpacing`` (0028|0030) / ``SliceThickness`` (0018|0050) tags — exactly
    what ``ImageSeriesReader`` reads back to reconstruct spacing/origin.
    """
    arr = np.arange(n * 5 * 4, dtype=np.int16).reshape(n, 5, 4)  # (z, y, x)
    vol = sitk.GetImageFromArray(arr)
    vol.SetSpacing(spacing)
    vol.SetOrigin(origin)
    vol.SetDirection((1, 0, 0, 0, 1, 0, 0, 0, 1))

    writer = sitk.ImageFileWriter()
    writer.KeepOriginalImageUIDOn()
    series_uid = "1.2.826.0.1.3680043.2.1125.9991"
    shared = [
        ("0008|0060", "CT"),
        ("0020|000e", series_uid),
        ("0008|0016", "1.2.840.10008.5.1.4.1.1.2"),
        ("0028|0030", f"{spacing[1]}\\{spacing[0]}"),  # PixelSpacing is row\col
        ("0018|0050", str(spacing[2])),
        ("0020|0037", "1\\0\\0\\0\\1\\0"),
    ]
    paths = []
    for i in range(n):
        sl = vol[:, :, i]
        for k, v in shared:
            sl.SetMetaData(k, v)
        pos = [origin[0], origin[1], origin[2] + i * spacing[2]]
        sl.SetMetaData("0020|0032", "\\".join(f"{p}" for p in pos))
        sl.SetMetaData("0020|0013", str(i + 1))
        sl.SetMetaData("0008|0018", f"1.2.3.4.{i + 1}")
        p = os.path.join(d, f"slice_{i:03d}.dcm")
        writer.SetFileName(p)
        writer.Execute(sl)
        paths.append(p)
    return vol, paths


def test_assembled_series_preserves_geometry():
    spacing = (0.7, 0.8, 2.5)  # x, y, z — anisotropic on purpose
    origin = (10.0, 20.0, 30.0)
    with tempfile.TemporaryDirectory() as d:
        src, paths = _write_synthetic_series(d, spacing, origin, n=5)
        out = os.path.join(d, "vol.assembled.nrrd")
        processing._assembleDicomToFile(paths, out)
        result = sitk.ReadImage(out)

    assert result.GetSpacing() == pytest.approx(spacing, abs=1e-4)
    assert result.GetOrigin() == pytest.approx(origin, abs=1e-4)
    assert result.GetDirection() == pytest.approx(src.GetDirection(), abs=1e-6)
    assert result.GetSize() == src.GetSize()
    # Regression guard: the per-slice path collapsed spacing to [1,1,1].
    assert result.GetSpacing() != (1.0, 1.0, 1.0)


def test_assemble_single_file_passes_through():
    with tempfile.TemporaryDirectory() as d:
        vol = sitk.GetImageFromArray(np.zeros((3, 4, 5), dtype=np.int16))
        vol.SetSpacing((1.5, 1.5, 4.0))
        vol.SetOrigin((1.0, 2.0, 3.0))
        src = os.path.join(d, "single.nrrd")
        sitk.WriteImage(vol, src)
        out = os.path.join(d, "single.assembled.nrrd")
        processing._assembleDicomToFile([src], out)
        result = sitk.ReadImage(out)

    assert result.GetSpacing() == pytest.approx((1.5, 1.5, 4.0))
    assert result.GetOrigin() == pytest.approx((1.0, 2.0, 3.0))


# ---------------------------------------------------------------------------
# Input-type dispatch parsing
# ---------------------------------------------------------------------------

_CLI_XML = """<?xml version="1.0"?>
<executable>
  <category>Radiology</category>
  <parameters>
    <image>
      <name>inputVolume</name>
      <channel>input</channel>
    </image>
    <image>
      <name>outputVolume</name>
      <channel>output</channel>
    </image>
    <directory>
      <name>inputDir</name>
    </directory>
    <file>
      <name>aux</name>
      <channel>input</channel>
    </file>
  </parameters>
</executable>
"""


def test_parse_cli_inputs_maps_input_params_to_tags():
    inputs = processing._parseCliInputs(_CLI_XML)
    assert inputs == {
        "inputVolume": "image",
        "inputDir": "directory",
        "aux": "file",
    }
    # Output params never appear as bindable inputs.
    assert "outputVolume" not in inputs


def test_parse_cli_inputs_handles_bad_xml():
    assert processing._parseCliInputs("not xml") == {}
    assert processing._parseCliInputs("") == {}


def test_task_binds_single_file_reads_declaration():
    twod = '<executable volview-dimensionality="2d"><title>x</title></executable>'
    slicey = '<executable volview-dimensionality="SLICE"></executable>'
    assert processing._taskBindsSingleFile(twod) is True
    assert processing._taskBindsSingleFile(slicey) is True
    # Default: no declaration → whole-volume assembly.
    assert processing._taskBindsSingleFile(_CLI_XML) is False
    assert processing._taskBindsSingleFile("") is False
    assert processing._taskBindsSingleFile("not xml") is False


# ---------------------------------------------------------------------------
# Dispatch in _translateValuesToSlicerParams
# ---------------------------------------------------------------------------

def _seriesRef(folderId=None, uid="1.2.3"):
    return processing.encodeSourceRef(
        seriesInstanceUID=uid, folderId=folderId or ObjectId()
    )


def test_image_input_series_ref_is_assembled_and_tracked(monkeypatch):
    ref = _seriesRef()
    files = [{"_id": ObjectId(), "name": "s1.dcm"}]
    staged = {"_id": ObjectId()}
    item = ObjectId()

    monkeypatch.setattr(
        processing, "resolveSeriesSourceRefToFiles", lambda r, user: files
    )
    calls = {}

    def fake_stage(fs, user, folder):
        calls["files"] = fs
        return str(staged["_id"]), str(item)

    monkeypatch.setattr(processing, "_stageAssembledVolume", fake_stage)

    params, transient = processing._translateValuesToSlicerParams(
        {"inputVolume": ref}, _CLI_XML, user=None, folder={"_id": ObjectId()}
    )

    assert params == {"inputVolume": str(staged["_id"])}
    assert transient == [str(item)]
    assert calls["files"] is files


def test_directory_input_series_ref_raises_b2_seam(monkeypatch):
    ref = _seriesRef()
    monkeypatch.setattr(
        processing,
        "resolveSeriesSourceRefToFiles",
        lambda r, user: pytest.fail("should not resolve before the dispatch check"),
    )
    with pytest.raises(RestException) as exc:
        processing._translateValuesToSlicerParams(
            {"inputDir": ref}, _CLI_XML, user=None, folder={"_id": ObjectId()}
        )
    assert exc.value.code == 501


def test_failure_after_staging_unwinds_orphaned_transients(monkeypatch):
    # An <image> series ref stages a volume, then a later <directory> series ref
    # raises the b2 501. No job exists yet to carry the staged id, so it must be
    # removed here rather than leaked.
    img_ref = _seriesRef(uid="1.1")
    dir_ref = _seriesRef(uid="2.2")
    staged_item = ObjectId()
    monkeypatch.setattr(
        processing, "resolveSeriesSourceRefToFiles",
        lambda r, user: [{"_id": ObjectId(), "name": "s.dcm"}],
    )
    monkeypatch.setattr(
        processing, "_stageAssembledVolume",
        lambda *a, **k: (str(ObjectId()), str(staged_item)),
    )
    removed = []
    monkeypatch.setattr(processing, "_removeTransientItems", removed.extend)

    with pytest.raises(RestException) as exc:
        processing._translateValuesToSlicerParams(
            {"inputVolume": img_ref, "inputDir": dir_ref},
            _CLI_XML, user=None, folder={"_id": ObjectId()},
        )

    assert exc.value.code == 501
    assert removed == [str(staged_item)]


def test_series_ref_on_undeclared_param_is_not_assembled(monkeypatch):
    # A series ref under a param the CLI does not declare as a file input must
    # not trigger a download/assemble/upload; it passes through as a string.
    ref = _seriesRef()
    monkeypatch.setattr(
        processing, "resolveSeriesSourceRefToFiles",
        lambda *a, **k: pytest.fail("undeclared param must not resolve a series"),
    )
    monkeypatch.setattr(
        processing, "_stageAssembledVolume",
        lambda *a, **k: pytest.fail("undeclared param must not assemble"),
    )

    params, transient = processing._translateValuesToSlicerParams(
        {"notAnInput": ref}, _CLI_XML, user=None, folder={"_id": ObjectId()}
    )

    assert params == {"notAnInput": ref}
    assert transient == []


def test_per_slice_task_binds_single_file_without_assembly(monkeypatch):
    ref = _seriesRef()
    files = [{"_id": ObjectId(), "name": "s1.dcm"}, {"_id": ObjectId()}]
    monkeypatch.setattr(
        processing, "resolveSeriesSourceRefToFiles", lambda r, user: files
    )
    monkeypatch.setattr(
        processing,
        "_stageAssembledVolume",
        lambda *a, **k: pytest.fail("per-slice task must not assemble"),
    )
    xml = '<executable volview-dimensionality="2d"><parameters><image>' \
        '<name>inputVolume</name><channel>input</channel></image></parameters>' \
        '</executable>'

    params, transient = processing._translateValuesToSlicerParams(
        {"inputVolume": ref}, xml, user=None, folder={"_id": ObjectId()}
    )

    assert params == {"inputVolume": str(files[0]["_id"])}
    assert transient == []


def test_plain_file_ref_unchanged(monkeypatch):
    fileId = ObjectId()
    monkeypatch.setattr(
        processing, "resolveSourceRefToFile", lambda v, user: {"_id": fileId}
    )

    params, transient = processing._translateValuesToSlicerParams(
        {"inputVolume": str(fileId)}, _CLI_XML, user=None, folder={"_id": ObjectId()}
    )

    assert params == {"inputVolume": str(fileId)}
    assert transient == []


def test_scalars_and_outputs_still_translate(monkeypatch):
    folder = {"_id": ObjectId()}
    params, transient = processing._translateValuesToSlicerParams(
        {
            "threshold": 42,
            "enabled": True,
            "outputVolume": {"name": "out.nii.gz"},
        },
        _CLI_XML, user=None, folder=folder,
    )
    assert params["threshold"] == "42"
    assert params["enabled"] == "true"
    assert params["outputVolume"] == "out.nii.gz"
    assert params["outputVolume_folder"] == str(folder["_id"])
    assert transient == []


# ---------------------------------------------------------------------------
# Job creation unwinds staged transients on failure (item 4.4)
# ---------------------------------------------------------------------------

def test_job_creation_failure_unwinds_staged_transients(monkeypatch):
    # Translate already staged a volume, then job creation throws *after*
    # translate returned (slicer_cli validation / docker down / token). No job
    # exists yet to carry the staged id, so _createCliJob must remove it here
    # rather than orphan a volviewTransient item hidden from the source list.
    staged = str(ObjectId())

    def boom(cliItem, params, user):
        raise RestException("docker unavailable", code=500)

    monkeypatch.setattr(processing, "_genDockerJob", boom)
    removed = []
    monkeypatch.setattr(processing, "_removeTransientItems", removed.extend)
    marked = []
    monkeypatch.setattr(
        processing, "_markJobTransients", lambda job, ids: marked.append(ids)
    )

    with pytest.raises(RestException) as exc:
        processing._createCliJob(
            cliItem=None, params={}, transientItemIds=[staged], user=None
        )

    assert exc.value.code == 500
    assert removed == [staged]  # staged volume reclaimed
    assert marked == []  # no job exists to carry the marker


def test_job_creation_happy_path_marks_transients(monkeypatch):
    # Success: the job is returned and carries the transient ids; nothing is
    # unwound (cleanup is the job's responsibility from here on).
    staged = str(ObjectId())
    job = {"_id": ObjectId()}
    monkeypatch.setattr(processing, "_genDockerJob", lambda c, p, u: job)
    removed = []
    monkeypatch.setattr(processing, "_removeTransientItems", removed.extend)
    marked = []
    monkeypatch.setattr(
        processing, "_markJobTransients", lambda j, ids: marked.append((j, ids))
    )

    out = processing._createCliJob(
        cliItem=None, params={}, transientItemIds=[staged], user=None
    )

    assert out is job
    assert removed == []
    assert marked == [(job, [staged])]


def test_job_creation_no_transients_marks_nothing(monkeypatch):
    # A job with no staged volumes (plain file input) creates normally and
    # marks nothing — the happy path is unchanged for the common case.
    job = {"_id": ObjectId()}
    monkeypatch.setattr(processing, "_genDockerJob", lambda c, p, u: job)
    marked = []
    monkeypatch.setattr(
        processing, "_markJobTransients", lambda j, ids: marked.append(ids)
    )

    out = processing._createCliJob(None, {}, [], user=None)

    assert out is job
    assert marked == []


# ---------------------------------------------------------------------------
# Transient cleanup on job completion
# ---------------------------------------------------------------------------

class _Event:
    def __init__(self, info):
        self.info = info


class _RecordingItemModel:
    def __init__(self, itemsById):
        self._items = itemsById
        self.removed = []

    def load(self, itemId, force=False):
        return self._items.get(str(itemId))

    def remove(self, item):
        self.removed.append(str(item["_id"]))


def _installItemModel(monkeypatch, itemIds):
    items = {str(i): {"_id": i} for i in itemIds}
    model = _RecordingItemModel(items)
    monkeypatch.setattr(processing, "Item", lambda: model)
    return model


class _FakeJobModel:
    def __init__(self, job):
        self._job = job

    def load(self, jobId, force=False):
        # The handler reloads the job from the DB by id; the committed doc
        # carries the marker/status regardless of the event's in-memory copy.
        if self._job is not None and str(jobId) == str(self._job.get("_id")):
            return self._job
        return None


def _installJobModel(monkeypatch, job):
    import girder_jobs.models.job as job_module
    monkeypatch.setattr(job_module, "Job", lambda: _FakeJobModel(job))


def _jobEvent(job):
    return _Event({"job": {"_id": job["_id"]}})


def test_cleanup_removes_transients_on_terminal_job(monkeypatch):
    a, b = ObjectId(), ObjectId()
    model = _installItemModel(monkeypatch, [a, b])
    job = {
        "_id": ObjectId(),
        "status": JobStatus.SUCCESS,
        "volviewTransient": [str(a), str(b)],
    }
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_jobEvent(job))

    assert sorted(model.removed) == sorted([str(a), str(b)])


def test_cleanup_skips_non_terminal_job(monkeypatch):
    a = ObjectId()
    model = _installItemModel(monkeypatch, [a])
    job = {
        "_id": ObjectId(),
        "status": JobStatus.RUNNING,
        "volviewTransient": [str(a)],
    }
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


def test_cleanup_noop_without_marker(monkeypatch):
    model = _installItemModel(monkeypatch, [])
    job = {"_id": ObjectId(), "status": JobStatus.SUCCESS}
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


def test_cleanup_reloads_job_so_stale_event_marker_is_ignored(monkeypatch):
    # The event carries an in-memory job dict WITHOUT the marker (the common
    # cross-process case); the reloaded DB doc has it, so cleanup still fires.
    a = ObjectId()
    model = _installItemModel(monkeypatch, [a])
    job = {
        "_id": ObjectId(),
        "status": JobStatus.SUCCESS,
        "volviewTransient": [str(a)],
    }
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_Event({"job": {"_id": job["_id"]}}))

    assert model.removed == [str(a)]


def test_cleanup_idempotent_when_item_already_gone(monkeypatch):
    a = ObjectId()
    model = _installItemModel(monkeypatch, [])  # item load returns None
    job = {
        "_id": ObjectId(),
        "status": JobStatus.ERROR,
        "volviewTransient": [str(a)],
    }
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


# ---------------------------------------------------------------------------
# Transient assembled volumes are not advertised as sources
# ---------------------------------------------------------------------------

class _FakeFolderModel:
    def __init__(self, items):
        self._items = items

    def childItems(self, folder, user=None, limit=0):
        return list(self._items)


class _FakeItemModel:
    def __init__(self, filesByItem):
        self._filesByItem = filesByItem

    def childFiles(self, item, limit=0):
        return list(self._filesByItem[item["_id"]])


def test_transient_assembled_item_is_skipped_in_sources(monkeypatch):
    real = {"_id": ObjectId(), "name": "brain.nii.gz", "meta": {}, "_files": None}
    transient = {
        "_id": ObjectId(),
        "name": "brain.assembled.nrrd",
        "meta": {"volviewTransient": True},
        "_files": None,
    }
    fReal = {"_id": ObjectId(), "name": "brain.nii.gz"}
    fTrans = {"_id": ObjectId(), "name": "brain.assembled.nrrd"}
    filesByItem = {real["_id"]: [fReal], transient["_id"]: [fTrans]}
    monkeypatch.setattr(
        processing, "Folder", lambda: _FakeFolderModel([real, transient])
    )
    monkeypatch.setattr(processing, "Item", lambda: _FakeItemModel(filesByItem))

    sources = processing._loadedSourcesForFolder({"_id": ObjectId()}, user=None)

    assert len(sources) == 1
    assert sources[0]["name"] == "brain.nii.gz"
