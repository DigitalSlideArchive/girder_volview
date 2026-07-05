"""Conformance for the ``{intents, missing}`` results-read envelope (Chunk 28).

The facade's ``getJobResults`` route returns the neutral result-read envelope
(contract Seam 3): a succeeded job yields ``{"intents": [...], "missing": N}``
(``jobResultsSchema``), and a non-success / total-loss read is a separate error
shape (``jobResultsErrorSchema``). Like the ``wire.spec.ts`` client suite, this
validates BOTH the shared golden fixtures AND a REAL facade-shaped payload
against the same generated JSON Schemas the contract publishes — one normative
definition, two validators (D4) — discharging the D12 obligation that every
published schema gets a validating consumer.

Offline (no Girder/Mongo): pure fixtures + schema. The live route payload is
validated end-to-end in ``test_job_output_binding_routes``.
"""

import jsonschema
import pytest

import contract_loader


def _validator(schema_name):
    """Build a Draft2020-12 validator for a generated schema.

    The generated schema is the facade-side stand-in for the normative ``zod``
    definition (D2/D4: internal conformance tooling, not the contract format).
    ``jsonschema`` is a hard test dependency (Chunk 29): a missing validator FAILS
    this conformance layer rather than silently skipping it.
    """
    schema = contract_loader.load_generated_schema(schema_name)
    return jsonschema.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# job-results.schema.json — the success envelope
# ---------------------------------------------------------------------------


def test_missing_fixture_validates_against_job_results_schema():
    # The golden fixture pins a PURE-intent envelope (contract vocabulary floor)
    # with an explicit missing count.
    fixture = contract_loader.load_fixture("wire/job-results.missing.json")
    _validator("job-results").validate(fixture)
    assert fixture["missing"] == 2
    assert len(fixture["intents"]) == 1


def test_hybrid_facade_payload_validates_against_job_results_schema():
    # The REAL facade route emits HYBRID items: the intent fields MERGED with the
    # id/mimeType/size file metadata the client's JobList reads (mimeType/size may
    # be null for an asset-store import). They fail the strict known-intent members
    # (additionalProperties: false) but pass the fail-open unknown-intent member's
    # catchall — so the envelope validates WITHOUT adding the metadata to the
    # contract schema (that would be a contract design change).
    payload = {
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
                # An asset-store import can lack these; the facade emits JSON null.
                "mimeType": None,
                "size": None,
                "source": {"jobId": "job-abc", "outputId": "outSeg"},
            },
        ],
        "missing": 1,
    }
    _validator("job-results").validate(payload)


def test_clean_success_envelope_with_zero_missing_validates():
    # missing is OPTIONAL + nonnegative; 0 (a clean success) is valid.
    _validator("job-results").validate({"intents": [], "missing": 0})


def test_envelope_without_missing_still_validates():
    # missing is optional: a facade that omits it stays backward-compatible.
    _validator("job-results").validate({"intents": []})


def test_envelope_missing_intents_is_rejected():
    # intents is REQUIRED — an object with only a count is not a valid envelope.
    validator = _validator("job-results")
    with pytest.raises(Exception):
        validator.validate({"missing": 2})


def test_envelope_negative_missing_is_rejected():
    validator = _validator("job-results")
    with pytest.raises(Exception):
        validator.validate({"intents": [], "missing": -1})


# ---------------------------------------------------------------------------
# job-results-error.schema.json — the non-success / total-loss error shape
# ---------------------------------------------------------------------------


def test_error_fixture_validates_against_job_results_error_schema():
    fixture = contract_loader.load_fixture("wire/job-results.error.json")
    _validator("job-results-error").validate(fixture)
    assert fixture["error"]
    assert fixture["state"] == "error"


def test_error_without_state_still_validates():
    # state is optional; error is the only required member.
    _validator("job-results-error").validate({"error": "no results"})


def test_error_without_message_is_rejected():
    validator = _validator("job-results-error")
    with pytest.raises(Exception):
        validator.validate({"state": "error"})


def test_error_with_state_outside_the_five_is_rejected():
    validator = _validator("job-results-error")
    with pytest.raises(Exception):
        validator.validate({"error": "x", "state": "queued"})
