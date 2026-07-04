"""Status-conformance for the neutral job-status projection (Chunk 23 reconcile).

The canonical ``processing-contract`` job-state enum was reconciled TO the runtime
names the facade already projects (``pending|running|success|error|cancelled``;
DECISIONS-LOG "Chunk 12 -> ORCHESTRATOR RESOLUTION", baked into Chunk 23). This
suite is the guard that keeps the facade's ``_projectJobStatus`` output and the
generated ``neutral-job-status`` schema from silently drifting apart again: EVERY
girder ``JobStatus`` the facade can observe must project to a state the generated
schema accepts, and the whole projected payload must validate.

Pure-unit (no server fixture / Mongo): it drives the pure projector against
hand-built job dicts and validates with the vendored generated JSON Schema.
"""

import pytest

import contract_loader
from girder_volview.facade.processing import _projectJobStatus


# Every girder job status the facade's state_map handles. Read via getattr so a
# renamed/removed girder status surfaces here as a collection error rather than a
# silently skipped case.
_STATUS_NAMES = ("INACTIVE", "QUEUED", "RUNNING", "SUCCESS", "ERROR", "CANCELED")


def _girder_statuses():
    from girder_jobs.constants import JobStatus
    return [(name, getattr(JobStatus, name)) for name in _STATUS_NAMES]


def _status_validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = contract_loader.load_generated_schema("neutral-job-status")
    return jsonschema.Draft202012Validator(schema)


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
    schema = contract_loader.load_generated_schema("neutral-job-status")
    allowed = schema["properties"]["state"]["enum"]
    assert _projectJobStatus(_job(status))["state"] in allowed


def test_generated_enum_is_the_reconciled_runtime_names():
    # The reconcile target: the canonical schema now carries the runtime names,
    # NOT the pre-reconcile queued|succeeded|failed spellings.
    schema = contract_loader.load_generated_schema("neutral-job-status")
    assert schema["properties"]["state"]["enum"] == [
        "pending",
        "running",
        "success",
        "error",
        "cancelled",
    ]


def test_every_neutral_state_is_reachable_from_some_girder_status():
    # The projection covers the full published enum (minus none): the five
    # runtime names are exactly what the facade can emit.
    schema = contract_loader.load_generated_schema("neutral-job-status")
    allowed = set(schema["properties"]["state"]["enum"])
    emitted = {_projectJobStatus(_job(status))["state"] for _, status in _girder_statuses()}
    assert emitted == allowed


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
