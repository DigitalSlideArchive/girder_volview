"""Status-conformance for the neutral job-status projection.

The canonical ``backend-contract`` job-state enum carries the runtime names the
backend projects (``pending|running|success|error|cancelled``). This suite guards
the backend's ``_projectJobStatus`` output and the generated
``neutral-job-status`` schema against silent drift: EVERY girder ``JobStatus``
the backend can observe must project to a state the generated schema accepts,
and the whole projected payload must validate.

Pure-unit (no server fixture / Mongo): it drives the pure projector against
hand-built job dicts and validates with the generated JSON Schema.
"""

import jsonschema
import pytest

import contract_loader
from girder_volview.backend.results import _projectJobStatus


# Every girder job status the backend's state_map handles. Read via getattr so a
# renamed/removed girder status surfaces here as a collection error rather than a
# silently skipped case.
_STATUS_NAMES = ("INACTIVE", "QUEUED", "RUNNING", "SUCCESS", "ERROR", "CANCELED")


def _girder_statuses():
    from girder_jobs.constants import JobStatus

    return [(name, getattr(JobStatus, name)) for name in _STATUS_NAMES]


def _status_validator():
    # Hard import: jsonschema is a declared test dep; a missing
    # validator FAILS the conformance layer, never silently skips it.
    schema = contract_loader.load_generated_schema("neutral-job-status")
    return jsonschema.Draft202012Validator(schema)


def _published_states():
    schema = contract_loader.load_generated_schema("neutral-job-status")
    return [variant["properties"]["state"]["const"] for variant in schema["oneOf"]]


def _job(status, log=None, progress=None):
    job = {"_id": "job-abc123", "status": status}
    if log is not None:
        job["log"] = log
    if progress is not None:
        job["progress"] = progress
    return job


@pytest.mark.parametrize("name,status", _girder_statuses())
def test_projected_status_validates_against_generated_schema(name, status):
    validator = _status_validator()
    validator.validate(_projectJobStatus(_job(status)))  # raises on drift


@pytest.mark.parametrize("name,status", _girder_statuses())
def test_projected_state_is_in_the_published_enum(name, status):
    assert _projectJobStatus(_job(status))["state"] in _published_states()


def test_generated_enum_is_the_reconciled_runtime_names():
    assert _published_states() == [
        "pending",
        "running",
        "success",
        "error",
        "cancelled",
    ]


def test_every_neutral_state_is_reachable_from_some_girder_status():
    allowed = set(_published_states())
    emitted = {
        _projectJobStatus(_job(status))["state"] for _, status in _girder_statuses()
    }
    assert emitted == allowed


def test_worker_active_states_project_to_running_and_validate():
    # girder_worker's CustomJobStatus active states (fetching/converting/pushing
    # input/output, canceling) must project to "running", never regress to
    # "pending": a polling client would otherwise see a running job go backwards.
    from girder_volview.backend.results import _workerActiveStates

    validator = _status_validator()
    active = _workerActiveStates()
    assert active  # the fallback (or the real import) is non-empty
    for status in active:
        projected = _projectJobStatus(_job(status))
        assert projected["state"] == "running"
        validator.validate(projected)


def test_error_projection_carries_errortail_and_validates():
    validator = _status_validator()
    from girder_jobs.constants import JobStatus

    projected = _projectJobStatus(_job(JobStatus.ERROR, log=["boom\n", "trace\n"]))
    assert projected["state"] == "error"
    assert projected["errorTail"]
    validator.validate(projected)


def test_running_projection_carries_progress_ratio_and_validates():
    validator = _status_validator()
    from girder_jobs.constants import JobStatus

    projected = _projectJobStatus(
        _job(JobStatus.RUNNING, progress={"current": 21, "total": 50})
    )
    assert projected["state"] == "running"
    assert projected["progress"] == pytest.approx(0.42)
    validator.validate(projected)


def test_over_100_percent_progress_is_clamped_to_one():
    # A worker/CLI reporting current > total must not emit a >1 ratio: the
    # client bounds progress to [0, 1] and would reject the whole history page.
    validator = _status_validator()
    from girder_jobs.constants import JobStatus

    projected = _projectJobStatus(
        _job(JobStatus.RUNNING, progress={"current": 105, "total": 100})
    )
    assert projected["progress"] == 1.0
    validator.validate(projected)


# Poll-load economy: the status load must not drag the unbounded job log on
# every ~2s poll — the log is (re)loaded only for a terminal-error projection.
def test_status_load_excludes_log_except_for_error(monkeypatch):
    import girder_jobs.models.job as job_module

    from girder_volview.backend import routes

    calls = []

    def _fakeJobModel(statuses):
        state = {"loads": 0}

        class _Model:
            def load(self, jobId, **kwargs):
                calls.append(bool(kwargs.get("includeLog")))
                status = statuses[min(state["loads"], len(statuses) - 1)]
                state["loads"] += 1
                job = {"_id": jobId, "status": status}
                if kwargs.get("includeLog"):
                    job["log"] = ["boom\n"]
                return job

        return _Model

    from girder_jobs.constants import JobStatus

    # Running job: ONE load, without the log.
    calls.clear()
    monkeypatch.setattr(job_module, "Job", _fakeJobModel([JobStatus.RUNNING]))
    job = routes._loadJobForStatusProjection("j1", user=None)
    assert calls == [False]
    assert "log" not in job

    # Error job: the log-less probe sees the error state and reloads WITH the
    # log so the bounded errorTail projection has its source.
    calls.clear()
    monkeypatch.setattr(job_module, "Job", _fakeJobModel([JobStatus.ERROR]))
    job = routes._loadJobForStatusProjection("j2", user=None)
    assert calls == [False, True]
    assert _projectJobStatus(job)["errorTail"] == "boom\n"
