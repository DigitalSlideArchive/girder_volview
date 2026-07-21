"""Offline unit coverage for the backend's neutral job projections.

Pure-stdlib (+ jsonschema), so it runs in every environment. The live-server
assertions (``listJobHistory`` scoping + observability bounds, the job-output
exclusion, still-downloadable-via-job) are in
``test_job_history_durability_routes`` and self-skip without Mongo.
"""

import datetime

import jsonschema
import pytest
from bson.objectid import ObjectId

import contract_loader
from girder_volview import handles, utils
from girder_volview.backend import inputs, results, routes
from girder_jobs.constants import JobStatus


def _installReadableFiles(monkeypatch, docs):
    """Model the batched output-file loader: canned file docs, every parent
    item readable. An id absent from ``docs`` models a deleted file."""
    byId = {}
    for doc in docs:
        doc.setdefault("itemId", ObjectId())
        byId[str(doc["_id"])] = doc

    class Files:
        def find(self, query, fields=None):
            wanted = {str(i) for i in query["_id"]["$in"]}
            return [d for k, d in byId.items() if k in wanted]

    class Items:
        def findWithPermissions(self, query, fields=None, user=None, level=None):
            return [{"_id": i} for i in query["_id"]["$in"]]

    monkeypatch.setattr(inputs, "File", Files)
    monkeypatch.setattr(inputs, "Item", Items)


def _ts(status, when):
    return {"status": status, "time": when}


# The wire ``finishedAt`` is ``_toIso(_terminalTime(job))``: the instant of the
# terminal status transition, on the server clock.
def test_terminal_time_is_the_terminal_transition_instant():
    from girder_jobs.constants import JobStatus

    t_run = datetime.datetime(2026, 7, 3, 18, 24, 0)
    t_done = datetime.datetime(2026, 7, 3, 18, 24, 5, 123000)
    job = {
        "_id": "j1",
        "timestamps": [
            _ts(JobStatus.RUNNING, t_run),
            _ts(JobStatus.SUCCESS, t_done),
        ],
    }
    # ISO-8601, UTC-tagged (Girder stores naive UTC) — parses as a UTC instant.
    finished = utils._toIso(results._terminalTime(job))
    assert finished == "2026-07-03T18:24:05.123000+00:00"


def test_terminal_time_none_for_never_terminal_job():
    from girder_jobs.constants import JobStatus

    job = {
        "_id": "j1",
        "timestamps": [
            _ts(JobStatus.QUEUED, datetime.datetime(2026, 7, 3, 18, 0, 0)),
            _ts(JobStatus.RUNNING, datetime.datetime(2026, 7, 3, 18, 1, 0)),
        ],
    }
    assert results._terminalTime(job) is None


def test_terminal_time_none_when_no_timestamps():
    assert results._terminalTime({"_id": "j1"}) is None


def test_terminal_time_reflects_error_and_cancelled_terminals():
    from girder_jobs.constants import JobStatus

    err = {
        "_id": "j",
        "timestamps": [_ts(JobStatus.ERROR, datetime.datetime(2026, 7, 3, 1, 2, 3))],
    }
    cancelled = {
        "_id": "j",
        "timestamps": [_ts(JobStatus.CANCELED, datetime.datetime(2026, 7, 3, 4, 5, 6))],
    }
    assert utils._toIso(results._terminalTime(err)) == "2026-07-03T01:02:03+00:00"
    assert utils._toIso(results._terminalTime(cancelled)) == "2026-07-03T04:05:06+00:00"


def _job_history_validator():
    schema = contract_loader.load_generated_schema("job-history-summary")
    return jsonschema.Draft202012Validator(schema)


def test_projected_job_history_summary_is_lightweight_and_schema_valid():
    from girder_jobs.constants import JobStatus
    from bson.objectid import ObjectId

    jid = ObjectId()
    job = {
        "_id": jid,
        "created": datetime.datetime(2026, 7, 3, 18, 24, 0),
        "title": "Otsu segmentation",
        "status": JobStatus.SUCCESS,
        inputs._TASK_ID_FIELD: "OtsuSegmentation",
        "timestamps": [
            _ts(JobStatus.RUNNING, datetime.datetime(2026, 7, 3, 18, 24, 1)),
            _ts(JobStatus.SUCCESS, datetime.datetime(2026, 7, 3, 18, 24, 5, 123000)),
        ],
    }
    user = {
        "_id": ObjectId(),
        "firstName": "Ada",
        "lastName": "Lovelace",
    }
    summary = results._projectJobHistorySummary(job, user)
    assert summary["jobId"] == str(jid)
    assert summary["taskTitle"] == "Otsu segmentation"
    assert summary["createdBy"]["name"] == "Ada Lovelace"
    assert summary["state"] == "success"
    assert summary["outputSummary"] == {
        "recorded": 0,
        "missing": 0,
    }
    assert "inputUris" not in summary
    assert "log" not in summary
    assert "kwargs" not in summary
    _job_history_validator().validate(summary)


def test_job_history_page_bounds_and_cursor_tie_clause_fail_closed():
    from bson.objectid import ObjectId
    from girder.exceptions import RestException

    assert routes._jobHistoryPageSize(None) == 25
    assert routes._jobHistoryPageSize(1) == 1
    assert routes._jobHistoryPageSize(100) == 100
    for invalid in (0, 101, -1, "not-an-integer"):
        with pytest.raises(RestException):
            routes._jobHistoryPageSize(invalid)
    with pytest.raises(RestException):
        routes._decodeJobCursor("not-a-valid-cursor")

    created = datetime.datetime(2026, 7, 12, 12, 0, 0)
    jobId = ObjectId()
    cursor = routes._encodeJobCursor({"_id": jobId, "created": created})
    assert routes._jobCursorContinuation(cursor) == [
        {"created": {"$lt": created}},
        {"created": created, "_id": {"$lt": jobId}},
    ]


def test_job_history_output_summary_reports_registration_and_missing(monkeypatch):
    present = ObjectId()
    deleted = ObjectId()
    _installReadableFiles(monkeypatch, [{"_id": present}])
    job = {
        "_id": "job-1",
        "status": JobStatus.SUCCESS,
        "volviewOutputs": {"seg": str(present), "report": str(deleted)},
        "volviewOutputSpecs": [
            {"name": "seg", "tag": "image", "isLabel": True},
            {"name": "report", "tag": "image", "isLabel": False},
            {"name": "never-produced", "tag": "image", "isLabel": False},
        ],
    }
    assert results._outputSummary(job, {"_id": "user-1"}) == {
        "recorded": 1,
        "missing": 2,
    }


def test_job_history_batches_readable_output_files_for_the_page(monkeypatch):
    from girder_volview.backend import results

    from bson.objectid import ObjectId

    present_id = ObjectId()
    deleted_id = ObjectId()
    readable_item_id = ObjectId()
    calls = {"files": 0, "items": 0, "load": 0}

    class Files:
        def find(self, query, **kwargs):
            calls["files"] += 1
            assert set(query["_id"]["$in"]) == {present_id, deleted_id}
            return [{"_id": present_id, "itemId": readable_item_id}]

        def load(self, *args, **kwargs):
            calls["load"] += 1
            raise AssertionError("history projection must use the page batch")

    class Items:
        def findWithPermissions(self, query, **kwargs):
            calls["items"] += 1
            assert set(query["_id"]["$in"]) == {readable_item_id}
            return [{"_id": readable_item_id}]

    monkeypatch.setattr(inputs, "File", Files)
    monkeypatch.setattr(inputs, "Item", Items)
    job = {
        "_id": "job-1",
        "status": JobStatus.SUCCESS,
        "volviewOutputs": {
            "seg": str(present_id),
            "report": str(deleted_id),
            "undeclared": str(ObjectId()),
        },
        "volviewOutputSpecs": [
            {"name": "seg", "tag": "image", "isLabel": True},
            {"name": "report", "tag": "file", "isLabel": False},
            {"name": "never-produced", "tag": "image", "isLabel": False},
        ],
    }

    readable = results._readableOutputFilesForJobs([job, job], {"_id": "user-1"})
    facts = results._projectJobFacts(
        job, {"_id": "user-1"}, readableOutputFiles=readable
    )

    assert results._outputSummary(job, {"_id": "user-1"}, facts) == {
        "recorded": 1,
        "missing": 2,
    }
    assert calls == {"files": 1, "items": 1, "load": 0}


def test_report_only_output_is_not_artifact_registration_failure(monkeypatch):
    table = ObjectId()
    _installReadableFiles(monkeypatch, [{"_id": table}])
    job = {
        "_id": "report-job",
        "status": JobStatus.SUCCESS,
        "volviewOutputs": {"table": str(table)},
        "volviewOutputSpecs": [
            {"name": "table", "tag": "file", "isLabel": False},
        ],
    }
    assert results._outputSummary(job, {"_id": "user-1"}) == {
        "recorded": 1,
        "missing": 0,
    }


def test_mixed_labelmap_and_report_counts_every_declared_resolving_output(monkeypatch):
    seg = ObjectId()
    table = ObjectId()
    _installReadableFiles(monkeypatch, [{"_id": seg}, {"_id": table}])
    job = {
        "_id": "mixed-job",
        "status": JobStatus.SUCCESS,
        "volviewOutputs": {"seg": str(seg), "table": str(table)},
        "volviewOutputSpecs": [
            {"name": "seg", "tag": "image", "isLabel": True},
            {"name": "table", "tag": "file", "isLabel": False},
        ],
    }
    assert results._outputSummary(job, {"_id": "user-1"}) == {
        "recorded": 2,
        "missing": 0,
    }


def test_to_iso_tags_naive_utc_and_passes_through_none():
    assert utils._toIso(None) is None
    assert (
        utils._toIso(datetime.datetime(2026, 7, 3, 18, 24, 5, 123000))
        == "2026-07-03T18:24:05.123000+00:00"
    )


def test_legacy_manifest_carries_no_session_watermark(monkeypatch):
    # A manifest that selects a session zip carries `resources` ONLY, never a
    # `sessionSavedAt` field. Stub the api-root URL helper so no Girder server
    # is needed (``handles`` is where the file-url mint reads it).
    monkeypatch.setattr(utils, "getApiRoot", lambda: "api/v1")
    monkeypatch.setattr(handles, "getApiRoot", lambda: "api/v1")
    session_files = [
        (
            None,
            {
                "_id": "f1",
                "name": "study.volview.zip",
                "created": datetime.datetime(2026, 7, 3, 12, 0, 0),
            },
        )
    ]

    manifest = utils.filesToManifest(session_files, "folder1")

    assert set(manifest) == {"resources"}
    assert isinstance(manifest["resources"], list)


def test_is_job_output_folder_item_reads_the_folder_marker(monkeypatch):
    # Ownership is FOLDER-level: an item is a job output when its PARENT FOLDER
    # carries the volviewJobOutputFolder marker; the item's own meta is
    # irrelevant. A missing/absent parent folder fails toward NOT-a-job-output.
    folders = {
        "marked": {"_id": "marked", "meta": {utils.JOB_OUTPUT_FOLDER_META_KEY: True}},
        "plain": {"_id": "plain", "meta": {}},
    }

    class Folders:
        def load(self, folderId, **kwargs):
            return folders.get(folderId)

    monkeypatch.setattr(utils, "Folder", Folders)

    assert utils.isJobOutputFolderItem({"folderId": "marked"}) is True
    assert utils.isJobOutputFolderItem({"folderId": "plain"}) is False
    assert utils.isJobOutputFolderItem({"folderId": "missing"}) is False
    assert utils.isJobOutputFolderItem({}) is False
    assert utils.isJobOutputFolderItem(None) is False


def test_transient_staged_file_excluded_from_loadable_images(monkeypatch):
    # A staged (transient) input is working data for a job submission, never
    # launch data: isLoadableImage must exclude it so a reload cannot surface
    # an abandoned staged segmentation as an ordinary image.
    items = {
        "staged": {"_id": "staged", "meta": {"volviewTransient": True}},
        "plain": {"_id": "plain", "meta": {}},
    }

    class Items:
        def load(self, itemId, **kwargs):
            return items.get(itemId)

    class Folders:
        def load(self, folderId, **kwargs):
            return {"_id": folderId, "meta": {}}

    monkeypatch.setattr(utils, "Item", Items)
    monkeypatch.setattr(utils, "Folder", Folders)

    staged = {"name": "seg.seg.nrrd", "itemId": "staged", "mimeType": ""}
    plain = {"name": "scan.nrrd", "itemId": "plain", "mimeType": ""}
    assert utils.isTransientStagedFile(staged) is True
    assert utils.isTransientStagedFile(plain) is False
    assert utils.isLoadableImage(staged) is False
    assert utils.isLoadableImage(plain) is True


@pytest.mark.parametrize("marked, expected_count", [(False, 3), (True, 0)])
def test_manifest_reuses_parent_item_for_dicom_series(
    monkeypatch, marked, expected_count
):
    # A DICOM series is N files under ONE item; the manifest resolver loads that
    # parent item exactly once (itemCache reuse) and folds the job-output exclusion
    # in through isLoadableImage -> isJobOutputFolderFile, which reads the marker
    # off the item's PARENT FOLDER.
    item_id = "series-item"
    folder_id = "series-folder"
    item_loads = []

    def _itemDoc(loaded_id):
        return {
            "_id": loaded_id,
            "folderId": folder_id,
            "meta": {"dicom": {"Modality": "CT"}},
        }

    class Items:
        def load(self, loaded_id, **kwargs):
            item_loads.append(loaded_id)
            return _itemDoc(loaded_id)

        def findWithPermissions(self, query=None, **kwargs):
            # The batched cache prime: ONE find over the distinct parent items.
            ids = ((query or {}).get("_id") or {}).get("$in", [])
            item_loads.extend(ids)
            return [_itemDoc(i) for i in ids]

    def _folderDoc(loaded_id):
        return {
            "_id": loaded_id,
            "meta": {utils.JOB_OUTPUT_FOLDER_META_KEY: marked},
        }

    class Folders:
        def load(self, loaded_id, **kwargs):
            return _folderDoc(loaded_id)

        def find(self, query=None, **kwargs):
            ids = ((query or {}).get("_id") or {}).get("$in", [])
            return [_folderDoc(i) for i in ids]

    monkeypatch.setattr(utils, "Item", Items)
    monkeypatch.setattr(utils, "Folder", Folders)
    file_entries = [
        (
            "series/%d.dcm" % index,
            {
                "_id": "file-%d" % index,
                "itemId": item_id,
                "name": "%d.dcm" % index,
                "mimeType": "application/dicom",
            },
        )
        for index in range(3)
    ]

    selected = utils.singleVolViewZipOrImageFiles(file_entries, user="owner")

    assert len(selected) == expected_count
    # The parent item is loaded ONCE for the whole series regardless of the
    # folder-marker outcome (the exclusion hops item -> parent folder).
    assert item_loads == [item_id]
