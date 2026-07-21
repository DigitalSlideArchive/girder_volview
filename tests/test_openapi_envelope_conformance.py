"""Every hand-authored OpenAPI ENVELOPE schema gets a validating consumer
(no documentation-only schemas).

``test_openapi_conformance`` guards the published document's neutrality +
operation surface; this suite validates the request/response envelope component
schemas the client wraps the wire vocabulary in (``TaskSummary``,
``RunTaskRequest``, ``JobRef``, ``StageResponse``, ``JobResults``) against REAL
backend route payloads.

Single source: the envelope schemas are loaded from the SAME contract
``openapi.json`` under ``components.schemas`` (never hand-copied into this test),
and every ``$ref`` resolves against that same document — so this consumer can
never fork the contract. ``jsonschema`` is a hard test dep: a missing
validator FAILS this conformance layer, never silently skips it.

Pure-stdlib payload shapes (no server fixture / Mongo): the reference backend's
route bodies are small and deterministic, so they are reproduced here exactly as
the route handlers emit them (``TaskSummary`` is driven through the real
``_cliItemToSummary`` builder). The end-to-end live payloads are validated in the
server-fixture suites (``test_job_output_binding_routes`` etc.).
"""

import types

import jsonschema
import pytest

import contract_loader
from girder_volview.backend import submit


def _envelope_validator(component_name):
    """A Draft2020-12 validator for one ``components.schemas`` entry.

    The whole OpenAPI document is the validation resource, and a top-level
    ``$ref`` targets the component; nested ``$ref``s (e.g. ``RunTaskRequest`` ->
    ``InputValue``, ``JobRef`` -> ``NeutralJobStatus``) resolve against the SAME
    document.
    """
    doc = contract_loader.load_openapi()
    schema = dict(doc)
    schema["$ref"] = "#/components/schemas/%s" % component_name
    return jsonschema.Draft202012Validator(schema)


# The hand-authored envelope schemas (openapi.ts `envelopeComponentSchemas`)
# validated HERE, plus the JobResults results envelope. Every other published
# component is a wire schema validated by its own conformance suite (see the
# audit test below).
_ENVELOPES_VALIDATED_HERE = frozenset(
    {
        "TaskSummary",
        "RunTaskRequest",
        "JobRef",
        "StageResponse",
        "JobResults",
    }
)

# Wire components validated by the per-schema conformance suites over the SAME
# generated schema injected into openapi.json (one normative def, two
# validators). Listed here as the audit's other half so a newly published
# component with NO consumer trips `test_every_openapi_component_...` below.
_WIRE_COMPONENTS_VALIDATED_ELSEWHERE = {
    "InputValue": "test_input_value_resolution",
    "StageInputDescriptor": "test_contract_fixtures",
    "TaskSpec": "test_slicer_spec_translation",
    "NeutralJobStatus": "test_status_conformance",
    "JobHistorySummary": "test_job_history_durability(_routes)",
    "JobHistoryPage": "test_job_history_durability_routes",
    "JobHistoryDetail": "test_job_history_durability_routes",
    "ResultIntent": "test_result_intent",
    "JobResultsError": "test_job_results_conformance",
}


def _stub_cli_item():
    # Carries exactly the members `_cliItemToSummary` reads, so the REAL builder
    # produces the wire payload.
    return types.SimpleNamespace(
        _id="6600000000000000000000f1",
        name="OtsuSegmentation",
        item={"description": "Otsu threshold segmentation"},
        image="dsarchive/histomicstk:latest",
    )


def test_task_summary_from_real_builder_validates():
    summary = submit._cliItemToSummary(_stub_cli_item())
    # The advisory `dockerImage`/`description` hints ride through
    # `additionalProperties: true`; only id/title are required.
    _envelope_validator("TaskSummary").validate(summary)
    assert summary["id"] and summary["title"]


def test_task_summary_requires_id_and_title():
    validator = _envelope_validator("TaskSummary")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"title": "no id"})


def test_run_task_request_with_input_and_scalars_validates():
    # ProcessingValue = InputValue | string | number | boolean | array | null.
    # The InputValue member exercises the nested `$ref` resolution.
    input_value = contract_loader.load_fixture("wire/input-value.single-file.json")
    body = {
        "values": {
            "inputVolume": input_value,
            "threshold": 42,
            "ratio": 0.5,
            "method": "otsu",
            "smoothing": True,
            "bounds": [1, 2, 3],
            "unbound": None,
        }
    }
    _envelope_validator("RunTaskRequest").validate(body)


def test_run_task_request_accepts_empty_values_map():
    # A no-parameter task still sends the key, so an empty map must validate.
    _envelope_validator("RunTaskRequest").validate({"values": {}})


def test_run_task_request_requires_values():
    # `values` is contractually REQUIRED — the client always sends the key, even
    # for a no-parameter task. The reference backend's tolerance of an absent
    # body (`routes.py` `(body or {})`) is implementation leniency, NOT part of
    # the neutral surface: a backend author may rely on the key being present.
    with pytest.raises(jsonschema.ValidationError):
        _envelope_validator("RunTaskRequest").validate({})


def test_run_task_request_rejects_malformed_input_value():
    # A bound value that claims to be an InputValue but omits `uris` matches NONE
    # of the value oneOf branches (fail closed) — the nested $ref bites.
    validator = _envelope_validator("RunTaskRequest")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"values": {"in": {"type": "image"}}})


def test_run_task_request_rejects_unknown_top_level_member():
    validator = _envelope_validator("RunTaskRequest")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"values": {}, "taskId": "smuggled"})


def test_job_ref_bare_id_validates():
    # The reference backend returns exactly `{jobId}`; `status` is the OPTIONAL
    # born-terminal fast-path, so omitting it stays compatible.
    _envelope_validator("JobRef").validate({"jobId": "6600000000000000000000d0"})


def test_job_ref_with_born_terminal_status_validates():
    # A synchronous backend may inline a terminal NeutralJobStatus.
    status = contract_loader.load_fixture("wire/status.success.json")
    _envelope_validator("JobRef").validate({"jobId": "job-1", "status": status})


def test_job_ref_requires_job_id():
    validator = _envelope_validator("JobRef")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"status": {"state": "success"}})


def test_stage_response_validates():
    payload = {"uris": ["/api/v1/file/6600000000000000000000b1/proxiable/scan.nrrd"]}
    _envelope_validator("StageResponse").validate(payload)


def test_stage_response_empty_uris_is_rejected():
    # `minItems: 1` — the client mints no URI itself, so an empty response fails
    # closed (the input-values contract).
    validator = _envelope_validator("StageResponse")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"uris": []})


# Mirrors _collectJobResults: each item is the neutral intent carrying its
# required id + advisory mimeType/size metadata.
_HYBRID_RESULTS_PAYLOAD = {
    "resultState": "incomplete",
    "intents": [
        {
            "intent": "add-base-image",
            "url": "/api/v1/file/6600000000000000000000d1/proxiable/o.nii.gz",
            "name": "o.nii.gz",
            "id": "6600000000000000000000d1",
            "mimeType": "application/octet-stream",
            "size": 12345,
        },
        {
            "intent": "add-segment-group",
            "url": "/api/v1/file/6600000000000000000000d2/proxiable/s.seg.nrrd",
            "name": "s.seg.nrrd",
            "id": "6600000000000000000000d2",
            "mimeType": None,
            "size": None,
            "source": {
                "providerId": "girder-slicer-cli:folder-abc",
                "jobId": "job-abc",
                "outputId": "outSeg",
            },
        },
    ],
    "missing": 1,
}


def test_job_results_envelope_validates_against_openapi_component():
    # The openapi-embedded JobResults is the SAME generated job-results schema
    # (injected in openapi.ts), so validating a real payload against the openapi
    # copy confirms the published document carries a usable results schema.
    _envelope_validator("JobResults").validate(_HYBRID_RESULTS_PAYLOAD)


def test_job_results_missing_fixture_validates_against_openapi_component():
    fixture = contract_loader.load_fixture("wire/job-results.missing.json")
    _envelope_validator("JobResults").validate(fixture)


def test_job_results_rejects_an_intents_item_without_id():
    # The canonical result-list item requires a nonempty id, so a bare intent
    # inside `intents` fails here exactly as it does in the client wire parser.
    validator = _envelope_validator("JobResults")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            {
                "resultState": "ready",
                "intents": [{"intent": "add-base-image", "url": "/x", "name": "x"}],
                "missing": 0,
            }
        )


def test_every_openapi_component_has_a_validating_consumer():
    # Fail-closed drift guard (no documentation-only schemas): a newly published
    # component with no validating consumer here or in a per-schema suite trips
    # this assertion.
    published = set(contract_loader.load_openapi()["components"]["schemas"])
    covered = _ENVELOPES_VALIDATED_HERE | set(_WIRE_COMPONENTS_VALIDATED_ELSEWHERE)
    assert published == covered, {
        "unconsumed": sorted(published - covered),
        "stale_in_audit": sorted(covered - published),
    }
