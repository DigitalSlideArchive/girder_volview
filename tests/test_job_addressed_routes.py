"""Server-fixture coverage for job-addressed routes + cancel, against real Girder
job models + the live cherrypy pipeline.

Status and results are reachable by job id alone
(``/volview_processing/jobs/<id>[/results]``) -- the launch folder is not part of
a job's identity -- so the job's own ACL is the only boundary: status is
READ-gated, cancel is WRITE-gated. Cancel is best-effort: it projects the neutral
``cancelled`` for a live job, and reports an already-terminal job's real state
rather than fabricating one.

Needs a live pytest-girder server + Mongo; self-skips when the test Mongo is
unreachable so the offline gate stays green.
"""

from conftest import _reload, mongo_reachable

import pytest


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_job_output_binding_routes); "
    "unavailable offline",
)


STATUS_PATH = "/volview_processing/jobs/%s"
RESULTS_PATH = "/volview_processing/jobs/%s/results"
CANCEL_PATH = "/volview_processing/jobs/%s/cancel"


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


def _get(server, path, user):
    return server.request(
        path=path, method="GET", user=user, isJson=True, exception=True
    )


def _cancel(server, jobId, user):
    return server.request(
        path=CANCEL_PATH % jobId,
        method="POST",
        user=user,
        isJson=True,
        exception=True,
    )


@pytest.mark.plugin("volview")
def test_status_route_is_job_addressed_no_folder(server, owner):
    from girder_jobs.constants import JobStatus

    job = _makeJob(owner, status=JobStatus.RUNNING)

    resp = _get(server, STATUS_PATH % job["_id"], owner)

    assert resp.output_status.startswith(b"200")
    assert resp.json["jobId"] == str(job["_id"])
    assert resp.json["state"] == "running"
    assert resp.json["resultState"] == "waiting"


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
    assert resp.json["resultState"] == "unavailable"
    assert "FileNotFoundError" in resp.json["errorTail"]
    assert "trace line" in resp.json["errorTail"]


@pytest.mark.plugin("volview")
def test_results_route_is_job_addressed_no_folder(server, owner):
    # A job with no recorded outputs that is not succeeded returns the explicit
    # error, never a silent [].
    job = _makeJob(owner)  # INACTIVE
    resp = _get(server, RESULTS_PATH % job["_id"], owner)
    assert resp.output_status.startswith(b"409")
    assert resp.json["code"] == "results_not_ready"
    assert resp.json["resultState"] == "waiting"
    assert resp.headers["Retry-After"] == "2"


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


@pytest.mark.plugin("volview")
def test_cancel_of_succeeded_job_is_best_effort_not_fabricated(server, owner):
    from girder_jobs.constants import JobStatus

    job = _makeJob(owner, status=JobStatus.SUCCESS)

    resp = _cancel(server, job["_id"], owner)

    # Girder does not transition SUCCESS -> CANCELED, so cancel no-ops and we
    # honestly report the job's real terminal state (never a fake `cancelled`).
    assert resp.output_status.startswith(b"200")
    assert resp.json["state"] == "success"
    assert resp.json["resultState"] == "ready"
    assert _reload(job["_id"])["status"] == JobStatus.SUCCESS


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
