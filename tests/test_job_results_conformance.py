"""Conformance for the ``{intents, missing}`` results-read envelope.

The backend's ``getJobResults`` route returns the neutral result-read envelope
(the status/results contract): a succeeded job yields
``{"resultState": ..., "intents": [...], "missing": N}``
(``jobResultsSchema``), and a non-success / total-loss read is a separate error
shape (``jobResultsErrorSchema``). Both the shared golden fixtures and a
backend-shaped payload are validated against the generated JSON Schemas the
contract publishes.

Runs offline on pure fixtures + schema. The live route payload is validated
end-to-end in ``test_job_output_binding_routes``.
"""

import jsonschema
import pytest

import contract_loader


def _validator(schema_name):
    """Build a Draft2020-12 validator for a generated schema.

    The generated schema is the backend-side stand-in for the normative ``zod``
    definition. ``jsonschema`` is a hard test dependency: a missing validator
    FAILS this conformance layer rather than silently skipping it.
    """
    schema = contract_loader.load_generated_schema(schema_name)
    return jsonschema.Draft202012Validator(schema)


def test_missing_fixture_validates_against_job_results_schema():
    fixture = contract_loader.load_fixture("wire/job-results.missing.json")
    _validator("job-results").validate(fixture)
    assert fixture["missing"] == 2
    assert len(fixture["intents"]) == 1


def test_hybrid_backend_payload_validates_against_job_results_schema():
    # The backend route emits the intent fields plus the required id and advisory
    # mimeType/size the client's JobList reads. All three are declared fields of
    # the canonical result-list item, so each item validates directly against its
    # strict known-intent member rather than the fail-open catchall.
    payload = {
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
                # An asset-store import can lack these; the backend emits JSON null.
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
    _validator("job-results").validate(payload)


def test_clean_success_envelope_with_zero_missing_validates():
    _validator("job-results").validate(
        {"resultState": "ready", "intents": [], "missing": 0}
    )


def test_envelope_without_readiness_or_missing_is_rejected():
    assert list(_validator("job-results").iter_errors({"intents": []}))


def test_envelope_missing_intents_is_rejected():
    validator = _validator("job-results")
    with pytest.raises(Exception):
        validator.validate({"resultState": "incomplete", "missing": 2})


def test_envelope_negative_missing_is_rejected():
    validator = _validator("job-results")
    with pytest.raises(Exception):
        validator.validate({"resultState": "incomplete", "intents": [], "missing": -1})


def test_error_fixture_validates_against_job_results_error_schema():
    fixture = contract_loader.load_fixture("wire/job-results.error.json")
    _validator("job-results-error").validate(fixture)
    assert fixture["message"]
    assert fixture["code"] == "results_unavailable"
    assert fixture["state"] == "error"


def test_error_requires_typed_lifecycle_fields():
    assert list(_validator("job-results-error").iter_errors({"message": "no results"}))


def test_error_without_message_is_rejected():
    validator = _validator("job-results-error")
    with pytest.raises(Exception):
        validator.validate({"code": "results_unavailable", "state": "error"})


def test_error_with_state_outside_the_five_is_rejected():
    validator = _validator("job-results-error")
    with pytest.raises(Exception):
        validator.validate(
            {
                "code": "results_unavailable",
                "message": "x",
                "state": "queued",
                "resultState": "unavailable",
            }
        )
