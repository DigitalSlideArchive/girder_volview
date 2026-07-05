"""Server-fixture coverage for Chunk 19 tier-2 durability (D5), against real
Girder models + the live cherrypy pipeline. Complements the offline
``test_tier2_durability`` unit tests.

Proven here (needs a live pytest-girder Mongo; self-skips when unreachable so the
offline gate stays green, and must pass wherever Mongo is present):

1. *listRecentJobs is context-scoped, not all-user-jobs* — a job stamped with THIS
   launch folder is listed; one stamped with another folder is not; scoped to the
   requesting user; the folder ACL gates a stranger.
2. *No time window* — an old (back-dated) in-context job is still listed (D5:
   unbounded, `since` is transport-only).
3. *Handle shape incl. finishedAt* — the emitted handles match the NeutralJobHandle
   generated JSON Schema; a succeeded job carries a real terminal `finishedAt`, a
   running one an empty instant.
4. *Launch manifest carries sessionSavedAt iff a session zip is selected* (folder
   + item launches).
5. *Job-output files are absent from the launch manifest* (folder + item launches)
   yet stay durable + downloadable.
"""

import datetime
import io
import os
import socket

import jsonschema
import pytest

import contract_loader
from girder_volview import utils
from girder_volview.facade import processing


# ---------------------------------------------------------------------------
# Self-skip when no live test Mongo is reachable (mirrors the other route tests)
# ---------------------------------------------------------------------------

def _mongo_reachable(timeout=0.5):
    host, port = "localhost", 27017
    uri = os.environ.get("GIRDER_TEST_DB", "")
    if uri.startswith("mongodb://"):
        netloc = uri[len("mongodb://"):].split("/", 1)[0].split(",", 1)[0]
        if ":" in netloc:
            host, port_str = netloc.rsplit(":", 1)
            port = int(port_str) if port_str.isdigit() else port
        elif netloc:
            host = netloc
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_job_output_binding_routes); "
    "unavailable offline",
)


JOBS_PATH = "/folder/%s/volview_processing/jobs"
FOLDER_MANIFEST_PATH = "/folder/%s/volview"
ITEM_MANIFEST_PATH = "/item/%s/volview"


# ---------------------------------------------------------------------------
# Users / folders
# ---------------------------------------------------------------------------

@pytest.fixture
def owner(db):
    from girder.models.user import User
    return User().createUser(
        login="tier2owner", password="password123", firstName="A", lastName="B",
        email="tier2owner@example.com", admin=False,
    )


@pytest.fixture
def stranger(db):
    from girder.models.user import User
    return User().createUser(
        login="tier2stranger", password="password123", firstName="N", lastName="A",
        email="tier2stranger@example.com", admin=False,
    )


@pytest.fixture
def folderA(fsAssetstore, owner):
    from girder.models.folder import Folder
    return Folder().createFolder(
        owner, "studyA", parentType="user", creator=owner, public=False
    )


@pytest.fixture
def folderB(fsAssetstore, owner):
    from girder.models.folder import Folder
    return Folder().createFolder(
        owner, "studyB", parentType="user", creator=owner, public=False
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload(job):
    from girder_jobs.models.job import Job
    return Job().load(job["_id"], force=True)


def _makeStampedJob(owner, folder, taskId="OtsuSegmentation", uris=None, status=None):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job
    job = Job().createJob(title="t", type="volview_test", user=owner, public=False)
    processing._stampJobContext(job, folder, taskId, uris or [])
    if status is not None:
        path = {
            JobStatus.RUNNING: [JobStatus.QUEUED, JobStatus.RUNNING],
            JobStatus.SUCCESS: [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCESS],
        }[status]
        for s in path:
            job = Job().updateJob(_reload(job), status=s)
    return _reload(job)


def _upload(owner, folder, name, content=b"bytes"):
    from girder.models.upload import Upload
    return Upload().uploadFromFile(
        io.BytesIO(content), size=len(content), name=name,
        parentType="folder", parent=folder, user=owner,
    )


def _get(server, path, user, params=None):
    return server.request(
        path=path, method="GET", user=user, params=params or {},
        isJson=True, exception=True,
    )


def _handle_validator():
    # Hard import (Chunk 29): jsonschema is a declared test dep; a missing
    # validator FAILS the conformance layer, never silently skips it.
    schema = contract_loader.load_generated_schema("neutral-job-handle")
    return jsonschema.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# 1 + 2 + 3. listRecentJobs — context-scoped, unbounded, NeutralJobHandle shape
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_list_recent_jobs_is_context_scoped_not_all_user_jobs(
    server, owner, folderA, folderB
):
    uris = ["/api/v1/file/6600000000000000000000a1/proxiable/1-001.dcm"]
    jobA = _makeStampedJob(owner, folderA, uris=uris)
    _makeStampedJob(owner, folderB)  # a job in a DIFFERENT launch context

    resp = _get(server, JOBS_PATH % folderA["_id"], owner)
    assert resp.output_status.startswith(b"200")

    ids = {h["jobId"] for h in resp.json}
    assert str(jobA["_id"]) in ids
    # The folder-B job is this same user's job, but a different launch context —
    # scoping is by context, not "all my jobs".
    assert len(resp.json) == 1
    assert resp.json[0]["taskId"] == "OtsuSegmentation"
    assert resp.json[0]["inputUris"] == uris


@pytest.mark.plugin("volview")
def test_old_in_context_job_is_still_listed_no_time_cutoff(server, owner, folderA):
    from girder_jobs.models.job import Job
    fresh = _makeStampedJob(owner, folderA)
    old = _makeStampedJob(owner, folderA)
    # Back-date the "old" job a year — D5 pins an UNBOUNDED listing (no `since`
    # semantic window), so an old in-context job must still appear.
    Job().collection.update_one(
        {"_id": old["_id"]},
        {"$set": {"created": datetime.datetime(2025, 1, 1)}},
    )

    resp = _get(server, JOBS_PATH % folderA["_id"], owner)
    ids = {h["jobId"] for h in resp.json}
    assert str(fresh["_id"]) in ids
    assert str(old["_id"]) in ids


@pytest.mark.plugin("volview")
def test_handles_match_generated_schema_and_finished_at_gates_on_terminal(
    server, owner, folderA
):
    from girder_jobs.constants import JobStatus
    done = _makeStampedJob(owner, folderA, status=JobStatus.SUCCESS)
    running = _makeStampedJob(owner, folderA, status=JobStatus.RUNNING)

    resp = _get(server, JOBS_PATH % folderA["_id"], owner)
    validator = _handle_validator()
    byId = {}
    for handle in resp.json:
        # Exactly the NeutralJobHandle keys — no route, no JobStatus enum, no file
        # id. `state` (Chunk 27) is the neutral projected status, not girder's enum.
        assert set(handle) == {"jobId", "taskId", "inputUris", "finishedAt", "state"}
        validator.validate(handle)
        byId[handle["jobId"]] = handle

    # The succeeded job carries a real terminal instant; the running one is empty.
    assert byId[str(done["_id"])]["finishedAt"] != ""
    assert byId[str(running["_id"])]["finishedAt"] == ""
    # ...and the neutral `state` (Chunk 27) tracks the same lifecycle, from the
    # SAME map _projectJobStatus uses (neutral names, never girder's JobStatus).
    assert byId[str(done["_id"])]["state"] == "success"
    assert byId[str(running["_id"])]["state"] == "running"


@pytest.mark.plugin("volview")
def test_list_recent_jobs_scoped_to_requesting_user_and_folder_acl(
    server, owner, stranger, folderA
):
    _makeStampedJob(owner, folderA)
    # A stranger with no READ on the private launch folder is blocked by the
    # folder ACL (the route's modelParam) — never leaks another user's jobs.
    resp = _get(server, JOBS_PATH % folderA["_id"], stranger)
    assert resp.output_status.startswith(b"403")


# ---------------------------------------------------------------------------
# 4. Launch manifest — sessionSavedAt present iff a session zip is selected
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_folder_manifest_has_session_saved_at_only_with_a_session(
    server, owner, folderA, folderB
):
    from girder.models.file import File
    # folderA: a session zip is present → it is the sole selected resource and the
    # manifest carries its server-side `created` as the watermark. Read `created`
    # back from the DB (BSON stores millisecond precision, so the in-memory upload
    # doc's microseconds differ from what the manifest projects).
    session = _upload(owner, folderA, "study.volview.zip")
    created = File().load(session["_id"], force=True)["created"]
    respA = _get(server, FOLDER_MANIFEST_PATH % folderA["_id"], owner)
    assert respA.output_status.startswith(b"200")
    assert "sessionSavedAt" in respA.json
    assert respA.json["sessionSavedAt"] == utils._toIso(created)

    # folderB: only a loose image, no session → no watermark (attach-all parity).
    _upload(owner, folderB, "brain.nrrd")
    respB = _get(server, FOLDER_MANIFEST_PATH % folderB["_id"], owner)
    assert respB.output_status.startswith(b"200")
    assert "sessionSavedAt" not in respB.json


@pytest.mark.plugin("volview")
def test_item_manifest_has_session_saved_at_only_with_a_session(server, owner, folderA):
    from girder.models.file import File
    from girder.models.item import Item
    session = _upload(owner, folderA, "study.volview.zip")
    created = File().load(session["_id"], force=True)["created"]
    sessionItem = Item().load(session["itemId"], force=True)

    resp = _get(server, ITEM_MANIFEST_PATH % sessionItem["_id"], owner)
    assert resp.output_status.startswith(b"200")
    assert resp.json["sessionSavedAt"] == utils._toIso(created)


# ---------------------------------------------------------------------------
# 5. Job-output files excluded from the launch manifest, yet still durable
# ---------------------------------------------------------------------------

def _manifest_names(manifest):
    return {r["name"] for r in manifest["resources"]}


@pytest.mark.plugin("volview")
def test_folder_manifest_excludes_job_output_but_keeps_the_base(server, owner, folderA):
    from girder.models.file import File
    _upload(owner, folderA, "brain.nrrd")
    output = _upload(owner, folderA, "brain.otsu.seg.nrrd")
    # Mark the output's item exactly as the data.process handler does at upload.
    processing._tagJobOutputItem(output)

    resp = _get(server, FOLDER_MANIFEST_PATH % folderA["_id"], owner)
    names = _manifest_names(resp.json)
    assert "brain.nrrd" in names
    # One path only: the job output is NOT in the launch manifest (VolView's
    # native loadSegmentations never also grabs it).
    assert "brain.otsu.seg.nrrd" not in names
    # ...but it stays durable: the file still exists + is readable (re-fetched via
    # the job path, which the Chunk-17/18 results route serves).
    assert File().load(output["_id"], user=owner, level=0, exc=False) is not None


@pytest.mark.plugin("volview")
def test_item_launch_of_a_job_output_yields_no_loadable_resource(server, owner, folderA):
    from girder.models.item import Item
    output = _upload(owner, folderA, "brain.otsu.seg.nrrd")
    processing._tagJobOutputItem(output)
    outputItem = Item().load(output["itemId"], force=True)

    # Item launch path (downloadManifest) applies the SAME isLoadableImage
    # exclusion: the marked output contributes no loadable resource (only the
    # appended config.json remains).
    resp = _get(server, ITEM_MANIFEST_PATH % outputItem["_id"], owner)
    names = _manifest_names(resp.json)
    assert "brain.otsu.seg.nrrd" not in names
    assert names == {"config.json"}
