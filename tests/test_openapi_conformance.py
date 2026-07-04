"""The published OpenAPI is the facade's obligation surface (Chunk 23 WI2).

The neutral REST OpenAPI (single-source-generated in VolView, vendored here with
the fixtures + generated schemas) states exactly what a NON-girder facade must
implement. This is the facade-side half of the conformance kit: the vendored
document stays neutral (grep-tested for girder route/id/enum/URL leaks — AC1),
declares exactly the neutral client-invoked operations, and the reference facade
implements every one of them.

Pure-stdlib (no server fixture / Mongo): it reads the vendored JSON and checks
the module-level handlers exist.
"""

import json
import re

import contract_loader
from girder_volview.facade import processing

_OPENAPI = contract_loader.GENERATED_ROOT / "openapi.json"

# Neutral operationId -> the reference-facade handler that implements it. A NEW
# backend re-authors this mapping; for the reference facade every handler exists.
_OP_TO_HANDLER = {
    "listTasks": "listTasks",
    "getTaskSpec": "getTaskSpec",
    "runTask": "runTask",
    "listRecentJobs": "listRecentJobs",
    "stageInput": "stageInput",
    "getJob": "getJob",
    "getJobResults": "getJobResults",
    "cancelJob": "cancelJob",
}

# The neutrality gate: none of these girder-specifics may appear in the neutral
# document (mirrors the VolView-side openapi.spec.ts grep). `NeutralJobStatus` is
# allowed; the bare girder `JobStatus` enum name is not.
_FORBIDDEN = [
    ("girder route param folderId", re.compile(r"folderId")),
    ("girder api mount /api/v1", re.compile(r"/api/v1")),
    ("proxiable url shape", re.compile(r"proxiable", re.IGNORECASE)),
    ("girder mention", re.compile(r"girder", re.IGNORECASE)),
    ("slicer mention", re.compile(r"slicer", re.IGNORECASE)),
    ("backend task xml", re.compile(r"\bxml\b", re.IGNORECASE)),
    ("JobStatus enum name", re.compile(r"(?<!Neutral)JobStatus")),
    ("girder status INACTIVE", re.compile(r"\bINACTIVE\b")),
    ("girder status CANCELED", re.compile(r"\bCANCELED\b")),
    ("retired state succeeded", re.compile(r"succeeded", re.IGNORECASE)),
    ("retired state failed", re.compile(r"failed", re.IGNORECASE)),
    ("retired state queued", re.compile(r"\bqueued\b", re.IGNORECASE)),
]


def _load_openapi():
    return json.loads(_OPENAPI.read_text())


def _declared_operation_ids(doc):
    return {
        op["operationId"]
        for path_item in doc["paths"].values()
        for op in path_item.values()
    }


def test_openapi_is_vendored_and_is_openapi_3_1():
    assert _OPENAPI.exists()
    assert _load_openapi()["openapi"].startswith("3.1")


def test_openapi_declares_exactly_the_neutral_operations():
    assert _declared_operation_ids(_load_openapi()) == set(_OP_TO_HANDLER)


def test_reference_facade_implements_every_declared_operation():
    for op_id in _declared_operation_ids(_load_openapi()):
        handler = _OP_TO_HANDLER[op_id]
        assert callable(getattr(processing, handler)), handler


def test_job_addressed_routes_are_keyed_by_job_id_alone():
    doc = _load_openapi()
    for path, path_item in doc["paths"].items():
        for op in path_item.values():
            if op["operationId"] in ("getJob", "getJobResults", "cancelJob"):
                assert path.startswith("/jobs/{jobId}"), path
                assert "folderId" not in path


def test_openapi_leaks_nothing_girder_specific():
    serialized = json.dumps(_load_openapi())
    for label, pattern in _FORBIDDEN:
        assert not pattern.search(serialized), label
