"""Server-fixture coverage for Chunk 18 job-addressed routes + cancel (D5).

Exercised against real Girder job models + the live cherrypy pipeline:

1. *Folder-free addressing* -- status and results are reachable by job id alone
   (``/volview_processing/jobs/<id>[/results]``), no folder in the path. The
   launch folder is not part of a job's identity (D5).
2. *Cancel projects to the neutral ``cancelled``* -- ``POST .../cancel`` on a
   live (running) job drives Girder cancellation and the response projects the
   neutral ``cancelled`` state, with the job actually CANCELED in Mongo.
3. *Best-effort, never fabricated* -- cancelling an already-terminal (succeeded)
   job is a no-op that honestly reports ``success``, never a fake ``cancelled``.
4. *Fail closed on the job's own ACL* -- a non-owner cannot even read a private
   job's status (403), and a read-only viewer of a public job is blocked from
   cancelling it (cancel is WRITE-gated, status is READ-gated).

Like ``test_job_output_binding_routes`` this needs a live pytest-girder server +
Mongo; the module self-skips when the test Mongo is unreachable so the offline
gate stays green, and runs (and must pass) wherever Mongo is present.
"""

import os
import socket

import pytest


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


STATUS_PATH = "/volview_processing/jobs/%s"
RESULTS_PATH = "/volview_processing/jobs/%s/results"
CANCEL_PATH = "/volview_processing/jobs/%s/cancel"


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@pytest.fixture
def owner(db):
    from girder.models.user import User
    return User().createUser(
        login="jobowner", password="password123", firstName="Job", lastName="Owner",
        email="jobowner@example.com", admin=False,
    )


@pytest.fixture
def stranger(db):
    from girder.models.user import User
    return User().createUser(
        login="stranger", password="password123", firstName="No", lastName="Access",
        email="stranger@example.com", admin=False,
    )


# ---------------------------------------------------------------------------
# Job helpers -- real girder_jobs models, driven through their state machine
# ---------------------------------------------------------------------------

def _makeJob(user, public=False, status=None):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job
    job = Job().createJob(title="t", type="volview_test", user=user, public=public)
    if status is not None:
        # createJob lands INACTIVE; walk the valid transition path to `status`.
        path = {
            JobStatus.QUEUED: [JobStatus.QUEUED],
            JobStatus.RUNNING: [JobStatus.QUEUED, JobStatus.RUNNING],
            JobStatus.SUCCESS: [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCESS],
            JobStatus.ERROR: [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.ERROR],
        }[status]
        for s in path:
            job = Job().updateJob(job, status=s)
    return job


def _reload(jobId):
    from girder_jobs.models.job import Job
    return Job().load(jobId, force=True)


def _get(server, path, user):
    return server.request(path=path, method="GET", user=user, isJson=True, exception=True)


def _cancel(server, jobId, user, headers=None):
    # cancel is a cookie-auth write route behind @csrfProtect: a bare
    # `Sec-Fetch-Site: same-origin` vouches for the caller (no Origin needed),
    # exactly as test_csrf_routes proves for the other write routes.
    browser = headers if headers is not None else [("Sec-Fetch-Site", "same-origin")]
    return server.request(
        path=CANCEL_PATH % jobId, method="POST", user=user,
        additionalHeaders=browser, isJson=True, exception=True,
    )


# ---------------------------------------------------------------------------
# 1. Folder-free addressing -- status + results reachable by job id alone
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_status_route_is_job_addressed_no_folder(server, owner):
    from girder_jobs.constants import JobStatus
    job = _makeJob(owner, status=JobStatus.RUNNING)

    resp = _get(server, STATUS_PATH % job["_id"], owner)

    assert resp.output_status.startswith(b"200")
    assert resp.json["jobId"] == str(job["_id"])
    # RUNNING projects to the neutral `running` (no folder was needed to get here).
    assert resp.json["state"] == "running"


@pytest.mark.plugin("volview")
def test_status_route_includes_error_tail(server, owner):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job
    job = _makeJob(owner, status=JobStatus.RUNNING)
    job = Job().updateJob(
        job,
        log="FileNotFoundError: output.nii.gz\ntrace line\n",
        status=JobStatus.ERROR,
    )

    resp = _get(server, STATUS_PATH % job["_id"], owner)

    assert resp.output_status.startswith(b"200")
    assert resp.json["state"] == "error"
    assert "FileNotFoundError" in resp.json["errorTail"]
    assert "trace line" in resp.json["errorTail"]


@pytest.mark.plugin("volview")
def test_results_route_is_job_addressed_no_folder(server, owner):
    # A job with no recorded outputs that is not succeeded returns the explicit
    # Chunk-17 error (never a silent []), proving the folder-free results route
    # still routes to the preserved handler body.
    job = _makeJob(owner)  # INACTIVE
    resp = _get(server, RESULTS_PATH % job["_id"], owner)
    assert resp.output_status.startswith(b"400")


# ---------------------------------------------------------------------------
# 2. Cancel projects to the neutral `cancelled`
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_cancel_projects_to_cancelled(server, owner):
    from girder_jobs.constants import JobStatus
    job = _makeJob(owner, status=JobStatus.RUNNING)

    resp = _cancel(server, job["_id"], owner)

    assert resp.output_status.startswith(b"200")
    assert resp.json["jobId"] == str(job["_id"])
    assert resp.json["state"] == "cancelled"
    # And the job is really CANCELED in Mongo -- not just a cosmetic response.
    assert _reload(job["_id"])["status"] == JobStatus.CANCELED


# ---------------------------------------------------------------------------
# 3. Best-effort -- an already-terminal job is a no-op, never fabricated
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_cancel_of_succeeded_job_is_best_effort_not_fabricated(server, owner):
    from girder_jobs.constants import JobStatus
    job = _makeJob(owner, status=JobStatus.SUCCESS)

    resp = _cancel(server, job["_id"], owner)

    # Girder does not transition SUCCESS -> CANCELED, so cancel no-ops and we
    # honestly report the job's real terminal state (never a fake `cancelled`).
    assert resp.output_status.startswith(b"200")
    assert resp.json["state"] == "success"
    assert _reload(job["_id"])["status"] == JobStatus.SUCCESS


# ---------------------------------------------------------------------------
# 4. Fail closed on the job's own ACL
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_stranger_cannot_read_private_job_status(server, owner, stranger):
    job = _makeJob(owner, public=False)
    resp = _get(server, STATUS_PATH % job["_id"], stranger)
    # No folder shields it; the job's own ACL does -- a non-owner is blocked.
    assert resp.output_status.startswith(b"403")


@pytest.mark.plugin("volview")
def test_read_only_viewer_cannot_cancel(server, owner, stranger):
    from girder_jobs.constants import JobStatus
    # Public job: the stranger has READ (can see status) but not WRITE.
    job = _makeJob(owner, public=True, status=JobStatus.RUNNING)

    # READ is enough to see the status...
    status = _get(server, STATUS_PATH % job["_id"], stranger)
    assert status.output_status.startswith(b"200")

    # ...but cancel is WRITE-gated, so the read-only viewer is blocked (and the
    # job is left untouched).
    resp = _cancel(server, job["_id"], stranger)
    assert resp.output_status.startswith(b"403")
    assert _reload(job["_id"])["status"] == JobStatus.RUNNING


@pytest.mark.plugin("volview")
def test_cancel_is_csrf_guarded(server, owner):
    from girder_jobs.constants import JobStatus
    job = _makeJob(owner, status=JobStatus.RUNNING)

    # A cross-site POST is rejected by the CSRF guard before the model layer, so
    # the job stays RUNNING even though the owner would otherwise be authorized.
    resp = _cancel(server, job["_id"], owner, headers=[("Sec-Fetch-Site", "cross-site")])
    assert resp.output_status.startswith(b"403")
    assert _reload(job["_id"])["status"] == JobStatus.RUNNING
