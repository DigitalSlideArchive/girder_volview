"""Offline unit coverage for the transient-cleanup cluster around *staged*
inputs: the job-bound terminal cleanup, the submit-side job marking, and the TTL
orphan sweep.

These drive the pure control flow with fake Girder/Job models -- no live Girder
-- so they run in the offline gate. The real Mongo-backed lifecycle (stage route
-> job terminal -> delete; orphan *age* discrimination) lives in
``test_staging_routes``.
"""

import datetime
from conftest import _Event

import pytest
from bson.objectid import ObjectId

from girder.exceptions import RestException

from girder_jobs.constants import JobStatus

from girder_volview.backend import inputs
from girder_volview.utils import TRANSIENT_STAGED_META_KEY


class _RecordingItemModel:
    """Minimal Item() stand-in: load by id, find returns a canned list, remove
    records. ``capture`` (a list) receives the query passed to ``find`` so a test
    can assert on the TTL cutoff the sweep built."""

    def __init__(self, itemsById=None, found=None, capture=None):
        self._items = itemsById or {}
        self._found = found or []
        self._capture = capture
        self.removed = []

    def load(self, itemId, force=False):
        return self._items.get(str(itemId))

    def find(self, query):
        if self._capture is not None:
            self._capture.append(query)
        ids = ((query or {}).get("_id") or {}).get("$in")
        if ids is not None:
            # The batched transient-marker read: answer by id from itemsById.
            return [self._items[str(i)] for i in ids if str(i) in self._items]
        return list(self._found)

    def remove(self, item):
        self.removed.append(str(item["_id"]))


def _installItemModel(monkeypatch, model):
    monkeypatch.setattr(inputs, "Item", lambda: model)
    return model


class _FakeJobModel:
    def __init__(self, job=None, jobs=None):
        self._job = job
        self._jobs = list(jobs or [])
        self.updated = []

    def load(self, jobId, force=False, includeLog=True):
        if self._job is not None and str(jobId) == str(self._job.get("_id")):
            return self._job
        for j in self._jobs:
            if str(jobId) == str(j.get("_id")):
                return j
        return None

    def updateJob(self, job, otherFields=None):
        self.updated.append(otherFields)
        if otherFields:
            job.update(otherFields)
        return job


def _installJobModel(monkeypatch, job):
    import girder_jobs.models.job as job_module

    model = _FakeJobModel(job)
    monkeypatch.setattr(job_module, "Job", lambda: model)
    return model


def _jobEvent(job):
    # The common cross-process case: the event carries only the id, not the
    # committed marker -- the handler must reload the job to see it.
    return _Event({"job": {"_id": job["_id"]}})


def test_cleanup_removes_transients_on_every_terminal_job_state(monkeypatch):
    a, b = ObjectId(), ObjectId()
    for status in (JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED):
        model = _installItemModel(
            monkeypatch,
            _RecordingItemModel({str(a): {"_id": a}, str(b): {"_id": b}}),
        )
        job = {
            "_id": ObjectId(),
            "status": status,
            "volviewTransient": [str(a), str(b)],
        }
        _installJobModel(monkeypatch, job)

        inputs._cleanupTransientOnJobDone(_jobEvent(job))

        assert sorted(model.removed) == sorted([str(a), str(b)])


def test_cleanup_skips_non_terminal_job(monkeypatch):
    a = ObjectId()
    model = _installItemModel(monkeypatch, _RecordingItemModel({str(a): {"_id": a}}))
    job = {"_id": ObjectId(), "status": JobStatus.RUNNING, "volviewTransient": [str(a)]}
    _installJobModel(monkeypatch, job)

    inputs._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


def test_cleanup_noop_without_marker(monkeypatch):
    model = _installItemModel(monkeypatch, _RecordingItemModel())
    job = {"_id": ObjectId(), "status": JobStatus.SUCCESS}
    _installJobModel(monkeypatch, job)

    inputs._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


def test_cleanup_reloads_job_so_stale_event_marker_is_ignored(monkeypatch):
    # The event's in-memory job dict has NO marker; the reloaded committed doc
    # does, so cleanup still fires (self-contained for any terminal updater).
    a = ObjectId()
    model = _installItemModel(monkeypatch, _RecordingItemModel({str(a): {"_id": a}}))
    job = {"_id": ObjectId(), "status": JobStatus.SUCCESS, "volviewTransient": [str(a)]}
    _installJobModel(monkeypatch, job)

    inputs._cleanupTransientOnJobDone(_Event({"job": {"_id": job["_id"]}}))

    assert model.removed == [str(a)]


def test_cleanup_idempotent_when_item_already_gone(monkeypatch):
    a = ObjectId()
    model = _installItemModel(monkeypatch, _RecordingItemModel())  # load -> None
    job = {"_id": ObjectId(), "status": JobStatus.ERROR, "volviewTransient": [str(a)]}
    _installJobModel(monkeypatch, job)

    inputs._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


def test_cleanup_ignores_malformed_event(monkeypatch):
    model = _installItemModel(monkeypatch, _RecordingItemModel())
    _installJobModel(monkeypatch, {"_id": ObjectId(), "status": JobStatus.SUCCESS})

    inputs._cleanupTransientOnJobDone(_Event(None))
    inputs._cleanupTransientOnJobDone(_Event({"job": "not-a-dict"}))

    assert model.removed == []


# Age alone decides: no job ever depends on a staged original (submission copies
# its inputs), so the sweep carries no live-job claim logic.
def test_sweep_builds_ttl_query_and_removes(monkeypatch):
    now = datetime.datetime(2026, 7, 4, 12, 0, 0)
    o1, o2 = ObjectId(), ObjectId()
    captured = []
    model = _installItemModel(
        monkeypatch,
        _RecordingItemModel(found=[{"_id": o1}, {"_id": o2}], capture=captured),
    )
    folder = {"_id": ObjectId()}

    inputs._sweepOrphanTransients(folder, now=now)

    assert sorted(model.removed) == sorted([str(o1), str(o2)])
    query = captured[0]
    assert query["folderId"] == folder["_id"]
    assert query["meta.volviewTransient"] is True
    assert query["created"] == {"$lt": now - inputs._TRANSIENT_ORPHAN_TTL}


class _CopyingItemModel(_RecordingItemModel):
    """Item() stand-in that also fakes ``copyItem``/``childFiles``.

    ``filesByItemId`` maps item id -> the item's file docs; a copy mints fresh
    item/file ids with the same file names, mirroring Girder's ``copyItem``.
    """

    def __init__(self, itemsById=None, filesByItemId=None):
        super().__init__(itemsById=itemsById)
        self._files = {
            str(itemId): list(files) for itemId, files in (filesByItemId or {}).items()
        }
        self.copies = []  # (srcItemId, destFolderId, newItemId)

    def load(self, itemId, **kwargs):
        return self._items.get(str(itemId))

    def copyItem(self, item, creator=None, folder=None):
        newId = ObjectId()
        copied = dict(item, _id=newId)
        self._items[str(newId)] = copied
        self._files[str(newId)] = [
            {"_id": ObjectId(), "name": f["name"], "itemId": newId}
            for f in self._files.get(str(item["_id"]), [])
        ]
        self.copies.append((str(item["_id"]), str(folder["_id"]), str(newId)))
        return copied

    def childFiles(self, item):
        return list(self._files.get(str(item["_id"]), []))


def test_staged_inputs_are_copied_and_params_rewritten(monkeypatch):
    stagedItemId, durableItemId = ObjectId(), ObjectId()
    stagedFile = {"_id": ObjectId(), "name": "seg.seg.nrrd", "itemId": stagedItemId}
    durableFile = {"_id": ObjectId(), "name": "scan.nrrd", "itemId": durableItemId}
    model = _installItemModel(
        monkeypatch,
        _CopyingItemModel(
            itemsById={
                str(stagedItemId): {
                    "_id": stagedItemId,
                    "meta": {TRANSIENT_STAGED_META_KEY: True},
                },
                str(durableItemId): {"_id": durableItemId, "meta": {}},
            },
            filesByItemId={
                stagedItemId: [stagedFile],
                durableItemId: [durableFile],
            },
        ),
    )
    outputFolder = {"_id": ObjectId()}
    params = {
        "segmentation": str(stagedFile["_id"]),
        "inputVolume": str(durableFile["_id"]),
        "threshold": "0.5",
    }
    resolved = {
        "segmentation": [stagedFile],
        "inputVolume": [durableFile],
    }

    newParams, copied = inputs.copyStagedInputsIntoJobFolder(
        params, resolved, user=object(), outputFolder=outputFolder
    )

    assert len(model.copies) == 1
    srcId, destFolderId, newItemId = model.copies[0]
    assert srcId == str(stagedItemId)
    assert destFolderId == str(outputFolder["_id"])
    assert copied == [newItemId]
    copiedFileId = str(model.childFiles({"_id": ObjectId(newItemId)})[0]["_id"])
    assert newParams["segmentation"] == copiedFileId
    assert newParams["segmentation"] != str(stagedFile["_id"])
    assert newParams["inputVolume"] == str(durableFile["_id"])
    assert newParams["threshold"] == "0.5"
    # The original params dict was not mutated.
    assert params["segmentation"] == str(stagedFile["_id"])


def test_copy_raises_conflict_when_parent_item_vanished_mid_submit(monkeypatch):
    # Interleave: URI resolution loaded the staged parent, then a concurrent
    # sweep/delete removed it before the copy loop re-loads it. That must be a
    # 409 submit failure — never the durable no-remap path, which would publish
    # a job whose params reference the deleted original's file ids.
    goneItemId = ObjectId()
    goneFile = {"_id": ObjectId(), "name": "seg.seg.nrrd", "itemId": goneItemId}
    # The item map is EMPTY: every load returns None, as after the sweep.
    _installItemModel(monkeypatch, _CopyingItemModel())

    with pytest.raises(RestException) as excinfo:
        inputs.copyStagedInputsIntoJobFolder(
            {"segmentation": str(goneFile["_id"])},
            {"segmentation": [goneFile]},
            user=object(),
            outputFolder={"_id": ObjectId()},
        )

    assert excinfo.value.code == 409


def test_partial_copy_raises_conflict_not_bare_valueerror(monkeypatch):
    # ``copyItem`` must duplicate every child file; a partial copy (a file
    # added/removed concurrently between resolution and copy) is the same
    # input-changed race as a vanished parent and takes the same typed 409 —
    # never zip(strict=True)'s bare ValueError surfacing as an opaque 500.
    stagedItemId = ObjectId()
    files = [
        {"_id": ObjectId(), "name": "a.nrrd", "itemId": stagedItemId},
        {"_id": ObjectId(), "name": "b.nrrd", "itemId": stagedItemId},
    ]

    class _PartialCopyItemModel(_CopyingItemModel):
        def copyItem(self, item, creator=None, folder=None):
            copied = super().copyItem(item, creator=creator, folder=folder)
            self._files[str(copied["_id"])].pop()  # the copy came up one file short
            return copied

    _installItemModel(
        monkeypatch,
        _PartialCopyItemModel(
            itemsById={
                str(stagedItemId): {
                    "_id": stagedItemId,
                    "meta": {TRANSIENT_STAGED_META_KEY: True},
                }
            },
            filesByItemId={stagedItemId: files},
        ),
    )

    with pytest.raises(RestException) as excinfo:
        inputs.copyStagedInputsIntoJobFolder(
            {"segmentation": ",".join(str(f["_id"]) for f in files)},
            {"segmentation": files},
            user=object(),
            outputFolder={"_id": ObjectId()},
        )

    assert excinfo.value.code == 409


def test_shared_staged_input_copied_once_per_job_submission(monkeypatch):
    stagedItemId = ObjectId()
    stagedFile = {"_id": ObjectId(), "name": "seg.seg.nrrd", "itemId": stagedItemId}
    model = _installItemModel(
        monkeypatch,
        _CopyingItemModel(
            itemsById={
                str(stagedItemId): {
                    "_id": stagedItemId,
                    "meta": {TRANSIENT_STAGED_META_KEY: True},
                }
            },
            filesByItemId={stagedItemId: [stagedFile]},
        ),
    )
    params = {"a": str(stagedFile["_id"]), "b": str(stagedFile["_id"])}
    resolved = {"a": [stagedFile], "b": [stagedFile]}

    newParams, copied = inputs.copyStagedInputsIntoJobFolder(
        params, resolved, user=object(), outputFolder={"_id": ObjectId()}
    )

    assert len(model.copies) == 1
    assert len(copied) == 1
    assert newParams["a"] == newParams["b"]
    assert newParams["a"] != str(stagedFile["_id"])


def test_is_transient_item():
    assert inputs._isTransientItem({"meta": {"volviewTransient": True}}) is True
    assert inputs._isTransientItem({"meta": {}}) is False
    assert inputs._isTransientItem({}) is False
    assert inputs._isTransientItem(None) is False
