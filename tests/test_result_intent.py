"""Conformance for the declarative result intents the processing backend emits.

Results cross the wire as declarative *intents* the client's single applier
applies — never a ``role`` the client switches on. ``_collectJobResults`` builds
each output's intent via ``_intentForOutput``; a labelmap's segment names/colors
travel inside the ``.seg.nrrd`` file as embedded metadata the client reads, so
the backend sets no ``segments`` payload. An annotations file output is typed by
its declared ``fileExtensions`` alone -- the backend never sniffs content -- and
its label styles likewise ride inside the file.

This suite exercises the pure intent builder and validates BOTH the backend's
emitted intents and the shared golden fixtures against the same generated JSON
Schema (``result-intent.schema.json``) — one normative definition, two validators.
"""

import jsonschema
import pytest

import contract_loader
from girder_volview.backend.results import _intentForOutput


_URL = "/api/v1/file/6600000000000000000000e1/proxiable/otsu.nii.gz"
_NAME = "otsu.nii.gz"
_PROVIDER_ID = "girder-slicer-cli:folder-abc123"
_JOB_ID = "job-abc123"


def _out(tag, isLabel, name="outputLabelmap", fileExtensions=""):
    # One recorded ``volviewOutputSpecs`` entry (``outputs.py``); ``parse_cli``
    # has already lowercased ``fileExtensions``.
    return {
        "name": name,
        "tag": tag,
        "isLabel": isLabel,
        "fileExtensions": fileExtensions,
    }


_ANNOTATIONS_URL = (
    "/api/v1/file/6600000000000000000000e4/proxiable/rois.annotations.json"
)
_ANNOTATIONS_NAME = "rois.annotations.json"


def _annotationsOut(name="outputAnnotations"):
    return _out("file", False, name=name, fileExtensions=".annotations.json")


def _intent_validator():
    """A JSON Schema validator for the generated result-intent schema.

    The generated schema is the backend-side stand-in for the normative ``zod``
    ``resultIntentSchema`` (internal conformance tooling, not the contract
    format itself). ``jsonschema`` is a hard test dep: a missing validator FAILS
    this conformance layer, never silently skips it.
    """
    schema = contract_loader.load_generated_schema("result-intent")
    return jsonschema.Draft202012Validator(schema)


def test_labelmap_image_maps_to_add_segment_group():
    intent = _intentForOutput(
        _out("image", True), _URL, _NAME, _PROVIDER_ID, _JOB_ID
    )
    assert intent["intent"] == "add-segment-group"
    assert intent["url"] == _URL and intent["name"] == _NAME


def test_labelmap_wins_over_non_image_tag():
    # `isLabel` is checked before `tag`, so a labelmap file is a segment group.
    intent = _intentForOutput(
        _out("file", True), _URL, _NAME, _PROVIDER_ID, _JOB_ID
    )
    assert intent["intent"] == "add-segment-group"


def test_plain_image_maps_to_add_base_image():
    intent = _intentForOutput(
        _out("image", False), _URL, _NAME, _PROVIDER_ID, _JOB_ID
    )
    assert intent == {"intent": "add-base-image", "url": _URL, "name": _NAME}


def test_non_image_file_has_no_state_intent():
    intent = _intentForOutput(
        _out("file", False), _URL, _NAME, _PROVIDER_ID, _JOB_ID
    )
    assert intent == {"url": _URL, "name": _NAME}
    assert "intent" not in intent


def test_plain_file_extension_has_no_state_intent():
    # A file output declaring an ordinary extension stays an ordinary result.
    intent = _intentForOutput(
        _out("file", False, name="report", fileExtensions=".csv"),
        "/api/v1/file/6600000000000000000000e5/proxiable/report.csv",
        "report.csv",
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent == {
        "url": "/api/v1/file/6600000000000000000000e5/proxiable/report.csv",
        "name": "report.csv",
    }
    assert "intent" not in intent


def test_annotations_file_maps_to_add_annotations():
    intent = _intentForOutput(
        _annotationsOut(),
        _ANNOTATIONS_URL,
        _ANNOTATIONS_NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent["intent"] == "add-annotations"
    assert intent["url"] == _ANNOTATIONS_URL
    assert intent["name"] == _ANNOTATIONS_NAME


def test_annotations_intent_carries_source_tag():
    intent = _intentForOutput(
        _annotationsOut(name="derivedRois"),
        _ANNOTATIONS_URL,
        _ANNOTATIONS_NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent["source"] == {
        "providerId": _PROVIDER_ID,
        "jobId": _JOB_ID,
        "outputId": "derivedRois",
    }


def test_annotations_source_ids_are_stringified():
    from bson.objectid import ObjectId

    oid = ObjectId("6600000000000000000000ff")
    intent = _intentForOutput(
        _annotationsOut(), _ANNOTATIONS_URL, _ANNOTATIONS_NAME, oid, oid
    )
    assert intent["source"]["providerId"] == str(oid)
    assert intent["source"]["jobId"] == str(oid)
    assert isinstance(intent["source"]["providerId"], str)
    assert isinstance(intent["source"]["jobId"], str)


def test_annotations_extension_is_matched_within_a_comma_list():
    # `fileExtensions` is a comma-separated declaration; membership decides.
    intent = _intentForOutput(
        _out(
            "file",
            False,
            name="outputAnnotations",
            fileExtensions=".json,.annotations.json",
        ),
        _ANNOTATIONS_URL,
        _ANNOTATIONS_NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent["intent"] == "add-annotations"


def test_dotless_annotations_declaration_still_maps_to_add_annotations():
    # Submit prepends the missing leading dot before the declaration names the
    # output file, so a dotless declaration produces an annotations-named file;
    # classification must agree or that file arrives typed as an opaque result.
    intent = _intentForOutput(
        _out(
            "file",
            False,
            name="outputAnnotations",
            fileExtensions="annotations.json",
        ),
        _ANNOTATIONS_URL,
        _ANNOTATIONS_NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent["intent"] == "add-annotations"


def test_labelmap_wins_over_annotations_extension():
    # Branch order is isLabel -> annotations: an `<image type="label">` output
    # stays a segment group whatever its declared extensions claim.
    intent = _intentForOutput(
        _out(
            "image",
            True,
            name="outputLabelmap",
            fileExtensions=".annotations.json",
        ),
        _URL,
        _NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent["intent"] == "add-segment-group"


def test_image_output_declaring_annotations_stays_a_base_image():
    # Only a `<file>` output can be annotations; an image output is an image.
    intent = _intentForOutput(
        _out(
            "image",
            False,
            name="outputVolume",
            fileExtensions=".annotations.json",
        ),
        _URL,
        _NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent == {"intent": "add-base-image", "url": _URL, "name": _NAME}


def test_annotations_intent_carries_no_segments_or_labels():
    # Label styles ride inside the annotations file, like a labelmap's segment
    # metadata; the intent carries no payload beyond the file reference.
    intent = _intentForOutput(
        _annotationsOut(),
        _ANNOTATIONS_URL,
        _ANNOTATIONS_NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert set(intent) == {"intent", "url", "name", "source"}


def test_segment_group_carries_source_tag():
    intent = _intentForOutput(
        _out("image", True, name="outputLabelmap"),
        _URL,
        _NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent["source"] == {
        "providerId": _PROVIDER_ID,
        "jobId": _JOB_ID,
        "outputId": "outputLabelmap",
    }


def test_source_output_id_is_the_output_identifier():
    intent = _intentForOutput(
        _out("image", True, name="mySeg"),
        _URL,
        _NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    assert intent["source"]["outputId"] == "mySeg"


def test_job_id_is_stringified():
    from bson.objectid import ObjectId

    oid = ObjectId("6600000000000000000000ff")
    intent = _intentForOutput(
        _out("image", True), _URL, _NAME, _PROVIDER_ID, oid
    )
    assert intent["source"]["jobId"] == str(oid)
    assert isinstance(intent["source"]["jobId"], str)


def test_embedded_labelmap_carries_no_segments():
    intent = _intentForOutput(
        _out("image", True), _URL, _NAME, _PROVIDER_ID, _JOB_ID
    )
    assert "segments" not in intent


def test_base_image_and_ordinary_file_carry_no_source_or_segments():
    for out in (_out("image", False), _out("file", False)):
        intent = _intentForOutput(out, _URL, _NAME, _PROVIDER_ID, _JOB_ID)
        assert "source" not in intent
        assert "segments" not in intent


# The backend emits the embedded (no-`segments`) labelmap shape; the optional
# `segments` shape stays contract-valid and is covered by the fixture-schema
# check below. `_intentForOutput` emits the INTENT only; `_collectJobResults`
# later adds the file `id` (and mimeType/size). Inject a stand-in id so each
# emitted row is a full result-list item the id-required schema accepts.
_RESULT_ID = "6600000000000000000000ff"
_EMITTED_CASES = {
    "add-segment-group.embedded": {
        **_intentForOutput(
            _out("image", True), _URL, _NAME, _PROVIDER_ID, _JOB_ID
        ),
        "id": _RESULT_ID,
    },
    "add-base-image": {
        **_intentForOutput(
            _out("image", False), _URL, _NAME, _PROVIDER_ID, _JOB_ID
        ),
        "id": _RESULT_ID,
    },
    "add-annotations": {
        **_intentForOutput(
            _annotationsOut(),
            _ANNOTATIONS_URL,
            _ANNOTATIONS_NAME,
            _PROVIDER_ID,
            _JOB_ID,
        ),
        "id": _RESULT_ID,
    },
    "ordinary-file": {
        **_intentForOutput(
            _out("file", False), _URL, _NAME, _PROVIDER_ID, _JOB_ID
        ),
        "id": _RESULT_ID,
    },
}


@pytest.mark.parametrize("stem", sorted(_EMITTED_CASES))
def test_emitted_intent_validates_against_schema(stem):
    validator = _intent_validator()
    validator.validate(_EMITTED_CASES[stem])  # raises on invalid


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
    # add-polygon is outside the state vocabulary; the schema still accepts it
    # so the applier performs no state action instead of dropping the read.
    validator = _intent_validator()
    unknown = contract_loader.load_fixture("wire/intent.unknown.json")
    assert unknown["intent"] == "add-polygon"
    assert validator.is_valid(unknown)


def test_emitted_add_segment_group_matches_fixture_shape():
    # The backend's emitted labelmap intent has the same key set as the golden
    # embedded add-segment-group fixture the client validates.
    embedded = _intentForOutput(
        _out("image", True), _URL, _NAME, _PROVIDER_ID, _JOB_ID
    )
    embedded_fixture = contract_loader.load_fixture(
        "wire/intent.add-segment-group.embedded.json"
    )
    # The fixture is a full result-list item (carries `id`); the emitted INTENT
    # never does (the collector adds it), so compare modulo the id key.
    assert set(embedded) == set(embedded_fixture) - {"id"}


def test_emitted_add_annotations_matches_fixture_shape():
    emitted = _intentForOutput(
        _annotationsOut(),
        _ANNOTATIONS_URL,
        _ANNOTATIONS_NAME,
        _PROVIDER_ID,
        _JOB_ID,
    )
    fixture = contract_loader.load_fixture("wire/intent.add-annotations.json")
    assert set(emitted) == set(fixture) - {"id"}
