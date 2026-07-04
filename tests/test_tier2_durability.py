"""Offline unit coverage for Chunk 19 tier-2 durability helpers (D5).

Pure-stdlib (+ jsonschema) coverage of the facade's neutral projections — no
Girder/Mongo needed, so it runs in every environment. The live-server assertions
(``listRecentJobs`` scoping, the launch-manifest ``sessionSavedAt`` /
job-output exclusion, still-downloadable-via-job) are in
``test_tier2_durability_routes`` and self-skip without Mongo.

What is proven here:
  * the NeutralJobHandle projection matches the frozen golden shape AND validates
    against the generated JSON Schema (the facade emits exactly the wire shape);
  * ``finishedAt`` is the terminal status-transition instant (server clock),
    empty for a never-terminal job — the session-watermark comparand;
  * input opaque URIs are collected verbatim, deduped, type-agnostic;
  * the session-watermark + job-output marker helpers behave as the manifest
    path expects.
"""

import datetime

import pytest

import contract_loader
from girder_volview import utils
from girder_volview.facade import processing


# ---------------------------------------------------------------------------
# _collectInputUris — verbatim, deduped, order-preserving, type-agnostic
# ---------------------------------------------------------------------------

def test_collect_input_uris_flattens_bound_inputs_in_order():
    values = {
        "inVol": {"type": "image", "uris": ["/a/1.dcm", "/a/2.dcm"]},
        "inMask": {"type": "labelmap", "uris": ["/a/mask.nrrd"]},
    }
    assert processing._collectInputUris(values) == [
        "/a/1.dcm", "/a/2.dcm", "/a/mask.nrrd",
    ]


def test_collect_input_uris_dedups_and_skips_non_inputs():
    values = {
        "inVol": {"type": "image", "uris": ["/a/1.dcm", "/a/1.dcm"]},
        "threshold": 42,                 # scalar param — no uris
        "outVol": "brain.otsu.nii.gz",   # autofilled output filename — no uris
        "weird": {"type": "image"},      # malformed — no uris key
    }
    assert processing._collectInputUris(values) == ["/a/1.dcm"]


def test_collect_input_uris_empty_when_no_bound_inputs():
    assert processing._collectInputUris({"threshold": 1}) == []
    assert processing._collectInputUris(None) == []


# ---------------------------------------------------------------------------
# _projectFinishedAt — terminal status-transition instant (server clock)
# ---------------------------------------------------------------------------

def _ts(status, when):
    return {"status": status, "time": when}


def test_finished_at_is_the_terminal_transition_instant():
    from girder_jobs.constants import JobStatus
    t_run = datetime.datetime(2026, 7, 3, 18, 24, 0)
    t_done = datetime.datetime(2026, 7, 3, 18, 24, 5, 123000)
    job = {"_id": "j1", "timestamps": [
        _ts(JobStatus.RUNNING, t_run),
        _ts(JobStatus.SUCCESS, t_done),
    ]}
    # ISO-8601, UTC-tagged (Girder stores naive UTC) — parses as a UTC instant.
    assert processing._projectFinishedAt(job) == "2026-07-03T18:24:05.123000+00:00"


def test_finished_at_empty_for_never_terminal_job():
    from girder_jobs.constants import JobStatus
    job = {"_id": "j1", "timestamps": [
        _ts(JobStatus.QUEUED, datetime.datetime(2026, 7, 3, 18, 0, 0)),
        _ts(JobStatus.RUNNING, datetime.datetime(2026, 7, 3, 18, 1, 0)),
    ]}
    assert processing._projectFinishedAt(job) == ""


def test_finished_at_empty_when_no_timestamps():
    assert processing._projectFinishedAt({"_id": "j1"}) == ""


def test_finished_at_reflects_error_and_cancelled_terminals():
    from girder_jobs.constants import JobStatus
    err = {"_id": "j", "timestamps": [_ts(JobStatus.ERROR,
           datetime.datetime(2026, 7, 3, 1, 2, 3))]}
    cancelled = {"_id": "j", "timestamps": [_ts(JobStatus.CANCELED,
                 datetime.datetime(2026, 7, 3, 4, 5, 6))]}
    assert processing._projectFinishedAt(err) == "2026-07-03T01:02:03+00:00"
    assert processing._projectFinishedAt(cancelled) == "2026-07-03T04:05:06+00:00"


# ---------------------------------------------------------------------------
# _projectJobHandle — the NeutralJobHandle wire shape (golden + schema)
# ---------------------------------------------------------------------------

def _job_handle_validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = contract_loader.load_generated_schema("neutral-job-handle")
    return jsonschema.Draft202012Validator(schema)


def test_projected_handle_shape_matches_the_golden_fixture():
    from girder_jobs.constants import JobStatus
    from bson.objectid import ObjectId
    jid = ObjectId()
    job = {
        "_id": jid,
        processing._TASK_ID_FIELD: "OtsuSegmentation",
        processing._INPUT_URIS_FIELD: [
            "/api/v1/file/6600000000000000000000a1/proxiable/1-001.dcm",
            "/api/v1/file/6600000000000000000000a2/proxiable/1-002.dcm",
        ],
        "timestamps": [_ts(JobStatus.SUCCESS,
                       datetime.datetime(2026, 7, 3, 18, 24, 5, 123000))],
    }
    handle = processing._projectJobHandle(job)

    # Same key set as the frozen golden fixture (jobId + taskId + inputUris +
    # finishedAt) — the client never sees a route, the JobStatus enum, or a file id.
    fixture = contract_loader.load_fixture("wire/job-handle.json")
    assert set(handle) == set(fixture)
    assert handle["jobId"] == str(jid)
    assert handle["taskId"] == "OtsuSegmentation"
    assert handle["inputUris"] == fixture["inputUris"]
    assert handle["finishedAt"] == fixture["finishedAt"]
    # ...and validates against the generated JSON Schema.
    _job_handle_validator().validate(handle)


def test_projected_handle_defaults_are_schema_valid_for_a_bare_job():
    # A job missing the launch-context stamp (should not happen for facade jobs,
    # but must never crash the listing) projects to empty taskId/inputUris + an
    # empty finishedAt, all still schema-valid strings/arrays.
    from bson.objectid import ObjectId
    handle = processing._projectJobHandle({"_id": ObjectId()})
    assert handle["taskId"] == ""
    assert handle["inputUris"] == []
    assert handle["finishedAt"] == ""
    _job_handle_validator().validate(handle)


# ---------------------------------------------------------------------------
# _toIso + sessionSavedAtFromFiles — the launch-manifest watermark
# ---------------------------------------------------------------------------

def test_to_iso_tags_naive_utc_and_passes_through_none():
    assert utils._toIso(None) is None
    assert utils._toIso(
        datetime.datetime(2026, 7, 3, 18, 24, 5, 123000)
    ) == "2026-07-03T18:24:05.123000+00:00"


def test_session_saved_at_reads_created_of_a_session_zip():
    created = datetime.datetime(2026, 7, 3, 12, 0, 0)
    files = [(None, {"name": "study.volview.zip", "created": created})]
    assert utils.sessionSavedAtFromFiles(files) == "2026-07-03T12:00:00+00:00"


def test_session_saved_at_none_when_no_session_selected():
    files = [
        (None, {"name": "brain.nrrd", "created": datetime.datetime(2026, 1, 1)}),
        (None, {"name": "scan.nii.gz", "created": datetime.datetime(2026, 1, 2)}),
    ]
    assert utils.sessionSavedAtFromFiles(files) is None


def test_manifest_carries_session_saved_at_only_when_a_session_is_present(monkeypatch):
    # filesToManifest builds `resources` + (iff a session zip is in the file set)
    # a top-level `sessionSavedAt`. Stub the api-root URL helper so no Girder
    # server is needed.
    monkeypatch.setattr(utils, "getApiRoot", lambda: "api/v1")
    session_files = [(None, {
        "_id": "f1", "name": "study.volview.zip",
        "created": datetime.datetime(2026, 7, 3, 12, 0, 0),
    })]
    image_files = [(None, {
        "_id": "f2", "name": "brain.nrrd",
        "created": datetime.datetime(2026, 7, 3, 12, 0, 0),
    })]

    with_session = utils.filesToManifest(session_files, "folder1")
    without_session = utils.filesToManifest(image_files, "folder1")

    assert with_session["sessionSavedAt"] == "2026-07-03T12:00:00+00:00"
    assert "sessionSavedAt" not in without_session
    # `resources` is unchanged shape either way (+ the appended config.json).
    assert isinstance(with_session["resources"], list)


# ---------------------------------------------------------------------------
# isJobOutputItem — the launch-manifest exclusion marker
# ---------------------------------------------------------------------------

def test_is_job_output_item_reads_the_marker():
    assert utils.isJobOutputItem({"meta": {utils.JOB_OUTPUT_META_KEY: True}}) is True
    assert utils.isJobOutputItem({"meta": {}}) is False
    assert utils.isJobOutputItem({}) is False
    assert utils.isJobOutputItem(None) is False


def test_marker_field_name_is_volview_job_output():
    # In-flight decision (Chunk 19): the WORKORDER's recommended `volviewJobOutput`
    # marker name (no collision with the existing `volviewTransient`).
    assert utils.JOB_OUTPUT_META_KEY == "volviewJobOutput"
    assert utils.JOB_OUTPUT_META_KEY != processing._TRANSIENT_META_KEY
