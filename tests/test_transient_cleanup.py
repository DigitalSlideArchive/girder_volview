"""Offline unit coverage for the Chunk 14 transient-cleanup cluster (rebuilt).

Chunk 9 deleted the b1 transient cluster; Chunk 14 rebuilds it for *staged*
inputs (D10): the job-bound terminal cleanup, the submit-side job marking, and
the TTL orphan sweep. These drive the pure control flow with fake Girder/Job
models -- no live Girder, same spirit as ``test_input_value_resolution`` -- so
they run in the offline gate too. The real Mongo-backed lifecycle (stage route ->
job terminal -> delete; orphan *age* discrimination) lives in
``test_staging_routes``.
"""

import datetime

from bson.objectid import ObjectId

from girder_jobs.constants import JobStatus

from girder_volview.facade import processing


# ---------------------------------------------------------------------------
# Fakes (no live Girder)
# ---------------------------------------------------------------------------

class _Event:
    def __init__(self, info):
        self.info = info


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
        return list(self._found)

    def remove(self, item):
        self.removed.append(str(item["_id"]))


def _installItemModel(monkeypatch, model):
    monkeypatch.setattr(processing, "Item", lambda: model)
    return model


class _FakeJobModel:
    def __init__(self, job):
        self._job = job
        self.updated = []

    def load(self, jobId, force=False):
        if self._job is not None and str(jobId) == str(self._job.get("_id")):
            return self._job
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


# ---------------------------------------------------------------------------
# Terminal cleanup handler
# ---------------------------------------------------------------------------

def test_cleanup_removes_transients_on_terminal_job(monkeypatch):
    a, b = ObjectId(), ObjectId()
    model = _installItemModel(
        monkeypatch, _RecordingItemModel({str(a): {"_id": a}, str(b): {"_id": b}})
    )
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
    model = _installItemModel(monkeypatch, _RecordingItemModel({str(a): {"_id": a}}))
    job = {"_id": ObjectId(), "status": JobStatus.RUNNING, "volviewTransient": [str(a)]}
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


def test_cleanup_noop_without_marker(monkeypatch):
    model = _installItemModel(monkeypatch, _RecordingItemModel())
    job = {"_id": ObjectId(), "status": JobStatus.SUCCESS}
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


def test_cleanup_reloads_job_so_stale_event_marker_is_ignored(monkeypatch):
    # The event's in-memory job dict has NO marker; the reloaded committed doc
    # does, so cleanup still fires (self-contained for any terminal updater).
    a = ObjectId()
    model = _installItemModel(monkeypatch, _RecordingItemModel({str(a): {"_id": a}}))
    job = {"_id": ObjectId(), "status": JobStatus.SUCCESS, "volviewTransient": [str(a)]}
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_Event({"job": {"_id": job["_id"]}}))

    assert model.removed == [str(a)]


def test_cleanup_idempotent_when_item_already_gone(monkeypatch):
    a = ObjectId()
    model = _installItemModel(monkeypatch, _RecordingItemModel())  # load -> None
    job = {"_id": ObjectId(), "status": JobStatus.ERROR, "volviewTransient": [str(a)]}
    _installJobModel(monkeypatch, job)

    processing._cleanupTransientOnJobDone(_jobEvent(job))

    assert model.removed == []


def test_cleanup_ignores_malformed_event(monkeypatch):
    model = _installItemModel(monkeypatch, _RecordingItemModel())
    _installJobModel(monkeypatch, {"_id": ObjectId(), "status": JobStatus.SUCCESS})

    processing._cleanupTransientOnJobDone(_Event(None))
    processing._cleanupTransientOnJobDone(_Event({"job": "not-a-dict"}))

    assert model.removed == []


# ---------------------------------------------------------------------------
# Submit-side job marking
# ---------------------------------------------------------------------------

def test_mark_job_transients_sets_marker(monkeypatch):
    job = {"_id": ObjectId()}
    model = _installJobModel(monkeypatch, job)

    processing._markJobTransients(job, ["i1", "i2"])

    assert model.updated == [{"volviewTransient": ["i1", "i2"]}]


# ---------------------------------------------------------------------------
# Orphan sweep — query construction + removal wiring (age discrimination is a
# real-Mongo concern, covered in test_staging_routes)
# ---------------------------------------------------------------------------

def test_sweep_builds_ttl_query_and_removes(monkeypatch):
    now = datetime.datetime(2026, 7, 4, 12, 0, 0)
    o1, o2 = ObjectId(), ObjectId()
    captured = []
    model = _installItemModel(
        monkeypatch,
        _RecordingItemModel(found=[{"_id": o1}, {"_id": o2}], capture=captured),
    )
    folder = {"_id": ObjectId()}

    processing._sweepOrphanTransients(folder, now=now)

    assert sorted(model.removed) == sorted([str(o1), str(o2)])
    query = captured[0]
    assert query["folderId"] == folder["_id"]
    assert query["meta.volviewTransient"] is True
    assert query["created"] == {"$lt": now - processing._TRANSIENT_ORPHAN_TTL}


def test_orphan_ttl_is_24_hours():
    # Chunk 14 in-flight decision, logged: 24h module constant.
    assert processing._TRANSIENT_ORPHAN_TTL == datetime.timedelta(hours=24)


# ---------------------------------------------------------------------------
# Transient-marker helpers
# ---------------------------------------------------------------------------

def test_is_transient_item():
    assert processing._isTransientItem({"meta": {"volviewTransient": True}}) is True
    assert processing._isTransientItem({"meta": {}}) is False
    assert processing._isTransientItem({}) is False
    assert processing._isTransientItem(None) is False
