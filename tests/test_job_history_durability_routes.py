"""Server-fixture coverage for the backend's job-listing + manifest routes,
against real Girder models + the live cherrypy pipeline. Complements the
offline ``test_job_history_durability`` unit tests.

Needs a live pytest-girder Mongo; the module self-skips when it is unreachable.
"""

import datetime
import io
import time
from conftest import _reload, mongo_reachable

import jsonschema
import pytest

import contract_loader
from girder_volview.backend import inputs, routes


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_job_output_binding_routes); "
    "unavailable offline",
)


JOBS_PATH = "/folder/%s/volview_processing/jobs"
FOLDER_MANIFEST_PATH = "/folder/%s/volview"
ITEM_MANIFEST_PATH = "/item/%s/volview"


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


def _makeStampedJob(owner, folder, taskId="OtsuSegmentation", status=None):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    job = Job().createJob(
        title="t",
        type="volview_test",
        user=owner,
        public=False,
        otherFields={
            inputs._LAUNCH_FOLDER_FIELD: str(folder["_id"]),
            inputs._TASK_ID_FIELD: taskId,
        },
    )
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
        io.BytesIO(content),
        size=len(content),
        name=name,
        parentType="folder",
        parent=folder,
        user=owner,
    )


def _get(server, path, user, params=None):
    return server.request(
        path=path,
        method="GET",
        user=user,
        params=params or {},
        isJson=True,
        exception=True,
    )


def _handle_validator():
    schema = contract_loader.load_generated_schema("job-history-summary")
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.plugin("volview")
def test_job_history_history_index_exists_in_query_order(db, server):
    # The plugin load driven by the server fixture is the production index
    # installation boundary; the database fixture alone does not load plugins.
    from girder_jobs.models.job import Job

    deadline = time.monotonic() + 5
    while True:
        index = Job().collection.index_information().get("volview_job_history")
        if index is not None or time.monotonic() >= deadline:
            break
        time.sleep(0.01)

    assert index is not None
    assert index["key"] == [
        (inputs._LAUNCH_FOLDER_FIELD, 1),
        ("userId", 1),
        ("created", -1),
        ("_id", -1),
    ]


@pytest.mark.plugin("volview")
def test_list_recent_jobs_is_context_scoped_not_all_user_jobs(
    server, owner, folderA, folderB
):
    jobA = _makeStampedJob(owner, folderA)
    _makeStampedJob(owner, folderB)  # a job in a DIFFERENT launch context

    resp = _get(server, JOBS_PATH % folderA["_id"], owner)
    assert resp.output_status.startswith(b"200")

    ids = {h["jobId"] for h in resp.json["jobs"]}
    assert str(jobA["_id"]) in ids
    # The folder-B job is this same user's job, but a different launch context —
    # scoping is by context, not "all my jobs".
    assert len(resp.json["jobs"]) == 1
    assert resp.json["jobs"][0]["taskId"] == "OtsuSegmentation"
    assert "inputUris" not in resp.json["jobs"][0]


def _backdate(job, when):
    from girder_jobs.models.job import Job

    Job().collection.update_one({"_id": job["_id"]}, {"$set": {"created": when}})


@pytest.mark.plugin("volview")
def test_old_terminal_and_non_terminal_jobs_are_both_reachable(server, owner, folderA):
    """Non-terminal jobs list UNCONDITIONALLY however old (the reloaded panel
    adopts them into its poller), and terminal history is equally durable.
    """
    from girder_jobs.constants import JobStatus

    fresh_done = _makeStampedJob(owner, folderA, status=JobStatus.SUCCESS)
    old_done = _makeStampedJob(owner, folderA, status=JobStatus.SUCCESS)
    old_running = _makeStampedJob(owner, folderA, status=JobStatus.RUNNING)
    a_year_ago = datetime.datetime.utcnow() - datetime.timedelta(days=365)
    _backdate(old_done, a_year_ago)
    _backdate(old_running, a_year_ago)

    resp = _get(server, JOBS_PATH % folderA["_id"], owner)
    ids = {h["jobId"] for h in resp.json["jobs"]}
    assert str(fresh_done["_id"]) in ids
    # An in-flight job from a year ago is still the panel's business...
    assert str(old_running["_id"]) in ids
    # ...and terminal history is equally durable.
    assert str(old_done["_id"]) in ids


@pytest.mark.plugin("volview")
def test_default_page_bound_has_an_honest_continuation(server, owner, folderA):
    from girder_jobs.constants import JobStatus

    now = datetime.datetime.utcnow()
    terminal = []
    for i in range(27):
        job = _makeStampedJob(owner, folderA, status=JobStatus.SUCCESS)
        # Distinct in-window instants, oldest first: i=0 is the oldest.
        _backdate(
            job, now - datetime.timedelta(hours=48) + datetime.timedelta(minutes=i)
        )
        terminal.append(job)
    running = _makeStampedJob(owner, folderA, status=JobStatus.RUNNING)

    resp = _get(server, JOBS_PATH % folderA["_id"], owner)
    ids = [h["jobId"] for h in resp.json["jobs"]]
    assert len(ids) == 25
    assert str(running["_id"]) in ids
    assert ids[0] == str(running["_id"])
    assert ids[1] == str(terminal[-1]["_id"])
    assert resp.json["nextCursor"]
    tail = _get(
        server,
        JOBS_PATH % folderA["_id"],
        owner,
        params={"cursor": resp.json["nextCursor"]},
    ).json
    assert len(tail["jobs"]) == 3
    assert tail["nextCursor"] is None


@pytest.mark.plugin("volview")
def test_job_history_history_pages_every_job_once_newest_first(server, owner, folderA):
    """Paging is a browse bound, never a retention cutoff."""
    from girder_jobs.constants import JobStatus

    now = datetime.datetime.utcnow()
    expected = []
    for i in range(31):
        job = _makeStampedJob(owner, folderA, status=JobStatus.SUCCESS)
        created = now - datetime.timedelta(days=i * 2)
        _backdate(job, created)
        expected.append((created, str(job["_id"])))

    seen = []
    cursor = None
    while True:
        params = {"limit": 7}
        if cursor is not None:
            params["cursor"] = cursor
        response = _get(server, JOBS_PATH % folderA["_id"], owner, params=params)
        assert set(response.json) == {"jobs", "nextCursor"}
        seen.extend(summary["jobId"] for summary in response.json["jobs"])
        cursor = response.json["nextCursor"]
        if cursor is None:
            break

    expected_ids = [job_id for _, job_id in sorted(expected, reverse=True)]
    assert seen == expected_ids
    assert len(seen) == len(set(seen)) == 31


@pytest.mark.plugin("volview")
def test_job_history_cursor_breaks_equal_created_ties_by_id(server, owner, folderA):
    same_created = datetime.datetime.utcnow() - datetime.timedelta(days=10)
    jobs = [_makeStampedJob(owner, folderA) for _ in range(5)]
    for job in jobs:
        _backdate(job, same_created)

    seen = []
    cursor = None
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = _get(server, JOBS_PATH % folderA["_id"], owner, params=params).json
        seen.extend(row["jobId"] for row in page["jobs"])
        cursor = page["nextCursor"]
        if cursor is None:
            break

    assert seen == sorted((str(job["_id"]) for job in jobs), reverse=True)


@pytest.mark.plugin("volview")
def test_job_history_cursor_and_page_bounds_fail_closed(server, owner, folderA):
    from girder_volview.backend.routes import (
        JOB_HISTORY_PAGE_DEFAULT,
        JOB_HISTORY_PAGE_MAX,
    )

    assert JOB_HISTORY_PAGE_DEFAULT == 25
    assert JOB_HISTORY_PAGE_MAX == 100
    for _ in range(3):
        _makeStampedJob(owner, folderA)

    one = _get(server, JOBS_PATH % folderA["_id"], owner, params={"limit": 1})
    assert one.output_status.startswith(b"200")
    assert len(one.json["jobs"]) == 1
    maximum = _get(server, JOBS_PATH % folderA["_id"], owner, params={"limit": 100})
    assert maximum.output_status.startswith(b"200")
    assert len(maximum.json["jobs"]) == 3

    for params in (
        {"limit": 0},
        {"limit": 101},
        {"limit": "not-an-integer"},
        {"cursor": "not-a-valid-cursor"},
    ):
        rejected = _get(server, JOBS_PATH % folderA["_id"], owner, params=params)
        assert rejected.output_status.startswith(b"400")


@pytest.mark.plugin("volview")
def test_job_history_history_is_personal_even_in_shared_folder(
    server, owner, stranger, folderA
):
    """Reopen projects Girder jobs for only the current user."""
    from girder.models.folder import Folder

    Folder().setUserAccess(folderA, stranger, level=0, save=True)
    mine = _makeStampedJob(owner, folderA)
    _makeStampedJob(stranger, folderA)

    response = _get(server, JOBS_PATH % folderA["_id"], owner)
    assert [summary["jobId"] for summary in response.json["jobs"]] == [str(mine["_id"])]


@pytest.mark.plugin("volview")
def test_job_history_list_is_lightweight_summary(server, owner, folderA):
    """Logs and raw parameters are detail-only data."""
    job = _makeStampedJob(owner, folderA)
    from girder_jobs.models.job import Job

    Job().collection.update_one(
        {"_id": job["_id"]},
        {"$set": {"log": ["secret"], "kwargs": {"threshold": 42}}},
    )
    response = _get(server, JOBS_PATH % folderA["_id"], owner)
    summary = response.json["jobs"][0]
    assert set(summary) == {
        "jobId",
        "taskId",
        "taskTitle",
        "createdBy",
        "createdAt",
        "state",
        "resultState",
        "outputSummary",
    }
    assert "log" not in summary
    assert "kwargs" not in summary


@pytest.mark.plugin("volview")
def test_job_history_query_excludes_the_log_field(server, owner, folderA, monkeypatch):
    """The history page query must exclude the unbounded log.

    The summary projection never reads the log, but a chatty/failed CLI's log can
    be multi-MB; materializing it per job on every page is pure cost. Capture the
    fields projection the route passes to findWithPermissions and assert it excludes
    log (mirroring JobModel.load(includeLog=False)'s {'log': False})."""
    from girder_jobs.models.job import Job

    _makeStampedJob(owner, folderA)
    captured = {}
    original = Job.findWithPermissions

    def _spy(self, *args, **kwargs):
        captured["fields"] = kwargs.get("fields")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Job, "findWithPermissions", _spy)
    response = _get(server, JOBS_PATH % folderA["_id"], owner)
    assert response.json["jobs"]  # the job is still listed
    assert captured["fields"] == {"log": False}


@pytest.mark.plugin("volview")
def test_job_history_batch_preserves_readable_and_missing_counts(
    server, owner, folderA
):
    from girder.models.file import File
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    job = _makeStampedJob(owner, folderA, status=JobStatus.SUCCESS)
    present = _upload(owner, folderA, "present.nrrd")
    deleted = _upload(owner, folderA, "deleted.nrrd")
    Job().collection.update_one(
        {"_id": job["_id"]},
        {
            "$set": {
                "volviewOutputs": {
                    "present": present["_id"],
                    "deleted": deleted["_id"],
                },
                "volviewOutputSpecs": [
                    {"name": "present", "tag": "image", "isLabel": False},
                    {"name": "deleted", "tag": "image", "isLabel": False},
                ],
            }
        },
    )
    File().remove(File().load(deleted["_id"], force=True))

    response = _get(server, JOBS_PATH % folderA["_id"], owner)
    summary = next(
        row for row in response.json["jobs"] if row["jobId"] == str(job["_id"])
    )

    assert summary["outputSummary"] == {"recorded": 1, "missing": 1}
    assert summary["resultState"] == "incomplete"


@pytest.mark.plugin("volview")
def test_job_history_detail_reads_logs_and_parameters_on_demand(server, owner, folderA):
    """Sensitive/heavy fields require a job-addressed read."""
    job = _makeStampedJob(owner, folderA)
    from girder_jobs.models.job import Job

    Job().collection.update_one(
        {"_id": job["_id"]},
        {
            "$set": {
                "log": ["detail-only"],
                "volviewSubmittedParameters": {"threshold": 42},
                "kwargs": {"girderToken": "must-not-leak"},
            }
        },
    )
    response = _get(
        server,
        "/volview_processing/jobs/%s/detail" % job["_id"],
        owner,
    )
    assert response.json == {
        "jobId": str(job["_id"]),
        "log": ["detail-only"],
        "parameters": {"threshold": 42},
    }


@pytest.mark.plugin("volview")
def test_job_history_delete_is_explicit_job_mutation(server, owner, folderA):
    # A terminal job deletes cleanly; the nonterminal 409 guard (cancel first) is
    # covered in test_job_deletion_routes.
    from girder_jobs.constants import JobStatus

    job = _makeStampedJob(owner, folderA, status=JobStatus.SUCCESS)
    response = server.request(
        path="/volview_processing/jobs/%s" % job["_id"],
        method="DELETE",
        user=owner,
        # The contract declares 204 with no response representation.
        isJson=False,
        exception=True,
    )
    assert response.output_status.startswith(b"204")
    from girder_jobs.models.job import Job

    assert Job().load(job["_id"], force=True, exc=False) is None


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
    for handle in resp.json["jobs"]:
        # Exactly the JobHistorySummary keys — no route, no JobStatus enum, no file
        # id. `state` is the neutral projected status, not girder's enum.
        validator.validate(handle)
        byId[handle["jobId"]] = handle

    # The succeeded job carries a real terminal instant; the running one is empty.
    assert byId[str(done["_id"])]["finishedAt"] != ""
    assert "finishedAt" not in byId[str(running["_id"])]
    # The neutral `state` comes from the SAME map _projectJobStatus uses (neutral
    # names, never girder's JobStatus).
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


@pytest.mark.plugin("volview")
def test_manifests_carry_no_session_watermark(server, owner, folderA):
    # Even a launch that selects a session zip emits a plain `{resources}`
    # manifest — no `sessionSavedAt`, on the folder route and the item route alike.
    from girder.models.item import Item

    session = _upload(owner, folderA, "study.volview.zip")
    sessionItem = Item().load(session["itemId"], force=True)

    respFolder = _get(server, FOLDER_MANIFEST_PATH % folderA["_id"], owner)
    assert respFolder.output_status.startswith(b"200")
    assert "sessionSavedAt" not in respFolder.json

    respItem = _get(server, ITEM_MANIFEST_PATH % sessionItem["_id"], owner)
    assert respItem.output_status.startswith(b"200")
    assert "sessionSavedAt" not in respItem.json


@pytest.mark.plugin("volview")
def test_folder_manifest_excludes_job_output_but_keeps_the_base(server, owner, folderA):
    # A folder launch loads exactly the loadable images in the folder; the
    # job-output exclusion rides the resolver's isLoadableImage path (bases only).
    # Ownership is FOLDER-level: the output lives in a real, MARKED private output
    # subfolder, created exactly as runTask does.
    from girder.models.file import File

    _upload(owner, folderA, "brain.nrrd")
    outputFolder = routes._createJobOutputFolder(folderA, owner, "hist-exclude")
    output = _upload(owner, outputFolder, "brain.otsu.seg.nrrd")

    resp = _get(server, FOLDER_MANIFEST_PATH % folderA["_id"], owner)
    assert resp.output_status.startswith(b"200")
    manifest = resp.json
    names = {s.get("name") for s in manifest["resources"]}
    assert "brain.nrrd" in names
    # One path only: the job output (in the marked folder) is NOT a launch base
    # (results take the job path only).
    assert "brain.otsu.seg.nrrd" not in names
    # ...but it stays durable: the file still exists + is readable (re-fetched via
    # the job path, which the results route serves).
    assert File().load(output["_id"], user=owner, level=0, exc=False) is not None


@pytest.mark.plugin("volview")
def test_item_launch_of_a_job_output_returns_empty_alive_manifest(
    server, owner, folderA
):
    from girder.models.item import Item

    outputFolder = routes._createJobOutputFolder(folderA, owner, "hist-itemopen")
    output = _upload(owner, outputFolder, "brain.otsu.seg.nrrd")
    outputItem = Item().load(output["itemId"], force=True)

    # The output stays excluded as a base (its parent folder is marked), but the
    # visible item-open affordance returns an empty-but-alive legacy manifest
    # rather than a 400. The config resource is scoped to the item's parent folder,
    # here the private output folder.
    resp = server.request(
        path=ITEM_MANIFEST_PATH % outputItem["_id"],
        method="GET",
        user=owner,
        isJson=True,
    )
    assert resp.output_status.startswith(b"200")
    assert resp.json == {
        "resources": [
            {
                "url": "/api/v1/folder/%s/volview_config/.volview_config.yaml"
                % outputFolder["_id"],
                "name": "config.json",
            }
        ],
    }
