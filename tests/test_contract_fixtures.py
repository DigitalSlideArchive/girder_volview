"""The backend loads the SAME backend-contract golden fixtures the VolView
client validates. Pure-stdlib load coverage — no Girder/Mongo needed. The
neutral contract carries only synthetic task-spec fixtures; backend-specific
source formats (e.g. Slicer XML) and their translated goldens are backend test
fixtures, exercised by ``test_slicer_spec_translation``.
"""

import json

import jsonschema

import contract_loader


def test_task_spec_fixtures_load():
    specs = contract_loader.load_fixture_dir("task-spec")
    assert set(specs) == {
        "synthetic-all-kinds",
        "synthetic-bounds-enum",
    }
    for spec in specs.values():
        assert spec["specVersion"] == 1
        assert spec["id"]
        assert spec["title"]
        assert isinstance(spec["parameters"], list)
        assert isinstance(spec["outputs"], list)


def test_source_ref_accepts_open_type_tags():
    spec = contract_loader.load_fixture("task-spec/synthetic-all-kinds.json")
    source_ref = next(p for p in spec["parameters"] if p["kind"] == "sourceRef")
    assert source_ref["accepts"] == ["image"]


def test_wire_fixtures_load():
    wire = contract_loader.load_fixture_dir("wire")
    assert wire["status.cancelled"]["state"] == "cancelled"
    assert wire["status.error-tail"]["errorTail"]
    assert wire["input-value.dicom-series"]["type"] == "image"
    assert len(wire["input-value.dicom-series"]["uris"]) > 1
    assert wire["input-value.labelmap"]["type"] == "labelmap"
    assert wire["stage-input.labelmap"]["type"] == "labelmap"
    assert wire["stage-input.labelmap"]["referenceImage"]["type"] == "image"
    assert wire["job-history-summary"]["state"] == "success"
    assert wire["job-history-page"]["nextCursor"]
    assert wire["job-history-detail"]["log"]
    assert wire["job-results.missing"]["missing"] == 2
    assert wire["job-results.error"]["state"] == "error"


def test_add_segment_group_variants_carry_source():
    with_segments = contract_loader.load_fixture(
        "wire/intent.add-segment-group.with-segments.json"
    )
    embedded = contract_loader.load_fixture(
        "wire/intent.add-segment-group.embedded.json"
    )
    assert with_segments["intent"] == "add-segment-group"
    assert with_segments["segments"]
    assert with_segments["source"] == {
        "providerId": "analysis-provider",
        "jobId": "job-abc123",
        "outputId": "outputLabelmap",
    }
    # bare seg.nrrd case: embedded metadata, no segments payload, but a source tag
    assert "segments" not in embedded
    assert embedded["source"]["outputId"] == "outputLabelmap"


def test_unknown_intent_fixture_present():
    unknown = contract_loader.load_fixture("wire/intent.unknown.json")
    assert unknown["intent"] == "add-polygon"
    assert unknown["url"] and unknown["name"]


def test_negative_fixtures_present():
    negatives = contract_loader.load_fixture_dir("negative")
    assert set(negatives) == {
        "unknown-field-kind",
        "constraint-violation",
        "wrong-length-color",
        "empty-uris",
    }


def test_input_value_schema_rejects_empty_uris():
    # Mirrors the backend's own 400 (inputs.resolveInputUrisToFiles): a bound
    # input with no uris is not a value, and the normative schema agrees.
    schema = contract_loader.load_generated_schema("input-value")
    empty = contract_loader.load_fixture("negative/empty-uris.json")
    assert not jsonschema.Draft202012Validator(schema).is_valid(empty)


def test_strict_intent_branch_rejects_wrong_length_color():
    # The tuple-length parity pin: the generated result-intent schema is
    # anyOf[strict known-intent branch, fail-open ordinary-result branch]. The
    # STRICT branch must close fixed-length tuples exactly like the normative
    # zod (minItems == maxItems == prefixItems length) — without that, a
    # wrong-length segments[].color passes the generated schema while the
    # client's zod demotes the row, defeating "one schema, two validators".
    schema = contract_loader.load_generated_schema("result-intent")
    strict = jsonschema.Draft202012Validator(schema["anyOf"][0])

    good = contract_loader.load_fixture(
        "wire/intent.add-segment-group.with-segments.json"
    )
    strict.validate(good)

    short = contract_loader.load_fixture("negative/wrong-length-color.json")
    assert not strict.is_valid(short)

    long = json.loads(json.dumps(good))
    long["segments"][0]["color"] = [255, 0, 0, 255, 255]
    assert not strict.is_valid(long)

    # The full union stays fail-open: the malformed row is still a readable
    # ordinary result (no state action), exactly like the client's demotion.
    jsonschema.Draft202012Validator(schema).validate(short)


def test_generated_schemas_present_and_parse():
    names = contract_loader.list_generated_schemas()
    assert "task-spec" in names
    assert "result-intent" in names
    assert "stage-input-descriptor" in names
    task_spec_schema = contract_loader.load_generated_schema("task-spec")
    assert task_spec_schema["type"] == "object"
    jsonschema.Draft202012Validator(
        contract_loader.load_generated_schema("stage-input-descriptor")
    ).validate(contract_loader.load_fixture("wire/stage-input.labelmap.json"))
