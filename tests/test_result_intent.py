"""Conformance for the declarative result intents the processing facade emits.

Results cross the wire as declarative *intents* the client's single applier
applies — never a ``role`` the client switches on (contract Seam 2; D3/D4).
``_collectJobResults`` builds each output's intent via ``_intentForOutput`` and
folds the ``_readLabelsSidecar`` labels into a labelmap intent's optional
``segments`` payload. This suite exercises the pure intent builder and, like the
VolView ``wire.spec.ts`` client suite, validates BOTH the facade's emitted
intents and the shared golden fixtures against the same generated JSON Schema
(``result-intent.schema.json``) — one normative definition, two validators.
"""

import jsonschema
import pytest

import contract_loader
from girder_volview.facade.processing import _intentForOutput


_URL = "/api/v1/file/6600000000000000000000e1/proxiable/otsu.nii.gz"
_NAME = "otsu.nii.gz"
_JOB_ID = "job-abc123"


def _out(tag, isLabel, name="outputLabelmap"):
    return {"name": name, "tag": tag, "isLabel": isLabel, "fileExtensions": ""}


def _intent_validator():
    """A JSON Schema validator for the generated result-intent schema.

    The generated schema is the facade-side stand-in for the normative ``zod``
    ``resultIntentSchema`` (D2/D4: internal conformance tooling, not the contract
    format itself). ``jsonschema`` is a hard test dep (Chunk 29): a missing
    validator FAILS this conformance layer, never silently skips it.
    """
    schema = contract_loader.load_generated_schema("result-intent")
    return jsonschema.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# The intent builder emits the v1 vocabulary with add-segment-group (NOT the
# retired attach-segment-group) and a source provenance tag.
# ---------------------------------------------------------------------------


def test_labelmap_image_maps_to_add_segment_group():
    intent = _intentForOutput(_out("image", True), _URL, _NAME, _JOB_ID)
    assert intent["intent"] == "add-segment-group"
    assert intent["url"] == _URL and intent["name"] == _NAME


def test_labelmap_wins_over_non_image_tag():
    # `isLabel` is checked before `tag`, so a labelmap file is a segment group.
    intent = _intentForOutput(_out("file", True), _URL, _NAME, _JOB_ID)
    assert intent["intent"] == "add-segment-group"


def test_plain_image_maps_to_add_base_image():
    intent = _intentForOutput(_out("image", False), _URL, _NAME, _JOB_ID)
    assert intent == {"intent": "add-base-image", "url": _URL, "name": _NAME}


def test_non_image_file_maps_to_download():
    intent = _intentForOutput(_out("file", False), _URL, _NAME, _JOB_ID)
    assert intent == {"intent": "download", "url": _URL, "name": _NAME}


# ---------------------------------------------------------------------------
# add-segment-group carries source:{jobId, outputId} and folds the labels
# sidecar into the optional `segments` payload (embedded metadata carries none).
# ---------------------------------------------------------------------------


def test_segment_group_carries_source_tag():
    intent = _intentForOutput(
        _out("image", True, name="outputLabelmap"), _URL, _NAME, _JOB_ID
    )
    assert intent["source"] == {"jobId": _JOB_ID, "outputId": "outputLabelmap"}


def test_source_output_id_is_the_output_identifier():
    intent = _intentForOutput(
        _out("image", True, name="mySeg"), _URL, _NAME, _JOB_ID
    )
    assert intent["source"]["outputId"] == "mySeg"


def test_job_id_is_stringified():
    from bson.objectid import ObjectId

    oid = ObjectId("6600000000000000000000ff")
    intent = _intentForOutput(_out("image", True), _URL, _NAME, oid)
    assert intent["source"]["jobId"] == str(oid)
    assert isinstance(intent["source"]["jobId"], str)


def test_folded_sidecar_becomes_segments_payload():
    segments = [
        {"value": 1, "name": "Bin 1", "color": [255, 0, 0, 255]},
        {"value": 2, "name": "Bin 2", "color": [0, 255, 0, 255]},
    ]
    intent = _intentForOutput(
        _out("image", True), _URL, _NAME, _JOB_ID, segments
    )
    assert intent["segments"] == segments


def test_embedded_labelmap_carries_no_segments():
    # A seg.nrrd with embedded metadata folds no sidecar -> no `segments`.
    intent = _intentForOutput(_out("image", True), _URL, _NAME, _JOB_ID)
    assert "segments" not in intent


def test_base_image_and_download_carry_no_source_or_segments():
    for out in (_out("image", False), _out("file", False)):
        intent = _intentForOutput(out, _URL, _NAME, _JOB_ID)
        assert "source" not in intent
        assert "segments" not in intent


# ---------------------------------------------------------------------------
# The emitted intents validate against the generated JSON Schema (the facade
# side of "both suites validate intent payloads against the same fixtures").
# ---------------------------------------------------------------------------

_EMITTED_CASES = {
    "add-segment-group.embedded": _intentForOutput(
        _out("image", True), _URL, _NAME, _JOB_ID
    ),
    "add-segment-group.with-segments": _intentForOutput(
        _out("image", True),
        _URL,
        _NAME,
        _JOB_ID,
        [{"value": 1, "name": "Bin 1", "color": [255, 0, 0, 255]}],
    ),
    "add-base-image": _intentForOutput(
        _out("image", False), _URL, _NAME, _JOB_ID
    ),
    "download": _intentForOutput(_out("file", False), _URL, _NAME, _JOB_ID),
}


@pytest.mark.parametrize("stem", sorted(_EMITTED_CASES))
def test_emitted_intent_validates_against_schema(stem):
    validator = _intent_validator()
    validator.validate(_EMITTED_CASES[stem])  # raises on invalid


# ---------------------------------------------------------------------------
# The shared golden fixtures validate against the same schema, exactly as the
# client's zod suite parses them — the single-source parity check. The unknown
# fixture (add-polygon) validates via the schema's fail-OPEN branch so the
# client applier can degrade it to `download` rather than reject the whole read.
# ---------------------------------------------------------------------------

_INTENT_FIXTURES = sorted(
    stem
    for stem in contract_loader.load_fixture_dir("wire")
    if stem.startswith("intent.")
)


@pytest.mark.parametrize("stem", _INTENT_FIXTURES)
def test_intent_fixture_validates_against_schema(stem):
    validator = _intent_validator()
    fixture = contract_loader.load_fixture("wire/{}.json".format(stem))
    validator.validate(fixture)


def test_unknown_intent_fixture_is_accepted_fail_open():
    # add-polygon is outside the v1 five; the schema still accepts it (fail-open)
    # so the applier degrades it to download instead of dropping the read.
    validator = _intent_validator()
    unknown = contract_loader.load_fixture("wire/intent.unknown.json")
    assert unknown["intent"] == "add-polygon"
    assert validator.is_valid(unknown)


def test_emitted_add_segment_group_matches_fixture_shape():
    # The facade's emitted labelmap intent has the same key set as the golden
    # add-segment-group fixtures (with/without segments) the client validates.
    with_segments = _intentForOutput(
        _out("image", True),
        _URL,
        _NAME,
        _JOB_ID,
        [{"value": 1, "name": "Bin 1", "color": [255, 0, 0, 255]}],
    )
    fixture = contract_loader.load_fixture(
        "wire/intent.add-segment-group.with-segments.json"
    )
    assert set(with_segments) == set(fixture)

    embedded = _intentForOutput(_out("image", True), _URL, _NAME, _JOB_ID)
    embedded_fixture = contract_loader.load_fixture(
        "wire/intent.add-segment-group.embedded.json"
    )
    assert set(embedded) == set(embedded_fixture)
