"""Every hand-authored OpenAPI ENVELOPE schema gets a validating consumer (Chunk
29; ARCHITECTURE-REVIEW §5.3 checklist item 2 "no documentation-only schemas").

``test_openapi_conformance`` guards the published document's neutrality + operation
surface; this suite closes the remaining gap it leaves: the request/response
envelope component schemas the client wraps the wire vocabulary in
(``TaskSummary``, ``RunTaskRequest``, ``JobRef``, ``StageResponse``,
``ResultListItem`` — plus the Chunk-28 ``JobResults`` results envelope) had NO
validating consumer. Each is now validated against REAL facade route payloads.

Single source (D4): the envelope schemas are loaded from the SAME vendored
``openapi.json`` under ``components.schemas`` (never hand-copied into this test),
and every ``$ref`` resolves against that same document — so this consumer can
never fork the contract. ``jsonschema`` is a hard test dep (Chunk 29): a missing
validator FAILS this conformance layer, never silently skips it.

Pure-stdlib payload shapes (no server fixture / Mongo): the reference facade's
route bodies are small and deterministic, so they are reproduced here exactly as
the route handlers emit them (``TaskSummary`` is driven through the real
``_cliItemToSummary`` builder). The end-to-end live payloads are validated in the
server-fixture suites (``test_job_output_binding_routes`` etc.).
"""

import json
import types

import jsonschema
import pytest

import contract_loader
from girder_volview.facade import processing


_OPENAPI = contract_loader.GENERATED_ROOT / "openapi.json"


def _load_openapi():
    return json.loads(_OPENAPI.read_text())


def _envelope_validator(component_name):
    """A Draft2020-12 validator for one ``components.schemas`` entry.

    The whole OpenAPI document is the validation resource, and a top-level
    ``$ref`` targets the component; nested ``$ref``s (e.g. ``RunTaskRequest`` ->
    ``InputValue``, ``JobRef`` -> ``NeutralJobStatus``) resolve against the SAME
    document. This loads the schema straight from the vendored contract — it does
    not hand-copy schema JSON (which would fork the single source).
    """
    doc = _load_openapi()
    schema = dict(doc)
    schema["$ref"] = "#/components/schemas/%s" % component_name
    return jsonschema.Draft202012Validator(schema)


# The five hand-authored envelope schemas (openapi.ts `envelopeComponentSchemas`)
# validated HERE, plus JobResults (the Chunk-28 results envelope, a wire component
# also read from openapi.json here). Every other published component is a wire
# schema validated by its own conformance suite (see the audit test below).
_ENVELOPES_VALIDATED_HERE = frozenset(
    {"TaskSummary", "RunTaskRequest", "JobRef", "StageResponse", "ResultListItem",
     "JobResults"}
)

# Wire components validated by the per-schema conformance suites over the SAME
# generated schema that is injected into openapi.json (one normative def, two
# validators; D4). Kept here as the audit's other half so a newly published
# component with NO consumer trips `test_every_openapi_component_...` below.
_WIRE_COMPONENTS_VALIDATED_ELSEWHERE = {
    "InputValue": "test_input_value_resolution",
    "TaskSpec": "test_slicer_spec_translation",
    "NeutralJobStatus": "test_status_conformance",
    "NeutralJobHandle": "test_tier2_durability(_routes)",
    "ResultIntent": "test_result_intent",
    "JobResultsError": "test_job_results_conformance",
}


# ---------------------------------------------------------------------------
# TaskSummary — the listTasks item, built by the REAL facade builder
# ---------------------------------------------------------------------------


def _stub_cli_item():
    # A minimal stand-in for a slicer_cli_web CLIItem carrying exactly the members
    # `_cliItemToSummary` reads, so the REAL builder produces the wire payload.
    return types.SimpleNamespace(
        _id="6600000000000000000000f1",
        name="OtsuSegmentation",
        item={"description": "Otsu threshold segmentation"},
        image="dsarchive/histomicstk:latest",
    )


def test_task_summary_from_real_builder_validates():
    summary = processing._cliItemToSummary(_stub_cli_item())
    # The advisory `dockerImage`/`description` hints ride through
    # `additionalProperties: true`; only id/title are required.
    _envelope_validator("TaskSummary").validate(summary)
    assert summary["id"] and summary["title"]


def test_task_summary_requires_id_and_title():
    validator = _envelope_validator("TaskSummary")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"title": "no id"})


# ---------------------------------------------------------------------------
# RunTaskRequest — the submit body the client POSTs (`{ values }`)
# ---------------------------------------------------------------------------


def test_run_task_request_with_input_and_scalars_validates():
    # A realistic submission: an InputValue-bound input plus scalar/list/null
    # params (ProcessingValue = InputValue | string | number | boolean | array |
    # null). The InputValue member exercises the nested `$ref` resolution.
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


@pytest.mark.parametrize("body", [{}, {"values": {}}])
def test_run_task_request_accepts_empty_submission(body):
    # `values` is optional and an empty map is valid — a pre-upgrade / no-param
    # submission stays compatible (additive rule).
    _envelope_validator("RunTaskRequest").validate(body)


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


# ---------------------------------------------------------------------------
# JobRef — the runTask response (`{ jobId }`, optional born-terminal `status`)
# ---------------------------------------------------------------------------


def test_job_ref_bare_id_validates():
    # The reference facade returns exactly `{jobId}` (runTask); `status` is the
    # OPTIONAL born-terminal fast-path, so omitting it stays compatible.
    _envelope_validator("JobRef").validate({"jobId": "6600000000000000000000d0"})


def test_job_ref_with_born_terminal_status_validates():
    # A synchronous backend may inline a terminal NeutralJobStatus; the nested
    # `$ref` to NeutralJobStatus resolves against the same document.
    status = contract_loader.load_fixture("wire/status.success.json")
    _envelope_validator("JobRef").validate({"jobId": "job-1", "status": status})


def test_job_ref_requires_job_id():
    validator = _envelope_validator("JobRef")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"status": {"state": "success"}})


# ---------------------------------------------------------------------------
# StageResponse — the stageInput response (`{ uris: [...] }`, >= 1, fail closed)
# ---------------------------------------------------------------------------


def test_stage_response_validates():
    # The shape stageInput returns: the facade-minted opaque download URI(s).
    payload = {"uris": ["/api/v1/file/6600000000000000000000b1/proxiable/scan.nrrd"]}
    _envelope_validator("StageResponse").validate(payload)


def test_stage_response_empty_uris_is_rejected():
    # `minItems: 1` — the client mints no URI itself, so an empty response fails
    # closed (Seam 1).
    validator = _envelope_validator("StageResponse")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"uris": []})


# ---------------------------------------------------------------------------
# JobResults + ResultListItem — the getJobResults `{ intents, missing }` envelope
# and its hybrid items, validated against the openapi-embedded copies
# ---------------------------------------------------------------------------


# The REAL facade results payload (mirrors _collectJobResults): each item is the
# neutral intent MERGED with advisory id/mimeType/size metadata — a ResultListItem.
_HYBRID_RESULTS_PAYLOAD = {
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
            "source": {"jobId": "job-abc", "outputId": "outSeg"},
        },
    ],
    "missing": 1,
}


def test_job_results_envelope_validates_against_openapi_component():
    # The openapi-embedded JobResults is the SAME generated job-results schema
    # (injected in openapi.ts); validating the real hybrid payload against the
    # openapi copy confirms the published document carries a usable results schema.
    _envelope_validator("JobResults").validate(_HYBRID_RESULTS_PAYLOAD)


def test_job_results_missing_fixture_validates_against_openapi_component():
    fixture = contract_loader.load_fixture("wire/job-results.missing.json")
    _envelope_validator("JobResults").validate(fixture)


def test_result_list_item_validates_for_each_hybrid_item():
    validator = _envelope_validator("ResultListItem")
    for item in _HYBRID_RESULTS_PAYLOAD["intents"]:
        validator.validate(item)  # id/name/url required; extra metadata allowed


def test_result_list_item_requires_id_name_url():
    validator = _envelope_validator("ResultListItem")
    with pytest.raises(jsonschema.ValidationError):
        # A bare intent (no id) is a ResultIntent, not a ResultListItem.
        validator.validate({"intent": "download", "url": "/x", "name": "x"})


# ---------------------------------------------------------------------------
# The AC2 audit guard: EVERY published component has a validating consumer
# ---------------------------------------------------------------------------


def test_every_openapi_component_has_a_validating_consumer():
    # Fail-closed drift guard for review §5.3 item 2 (no documentation-only
    # schemas): a newly published component with no validating consumer here or in
    # a per-schema suite trips this assertion.
    published = set(_load_openapi()["components"]["schemas"])
    covered = _ENVELOPES_VALIDATED_HERE | set(_WIRE_COMPONENTS_VALIDATED_ELSEWHERE)
    assert published == covered, {
        "unconsumed": sorted(published - covered),
        "stale_in_audit": sorted(covered - published),
    }
