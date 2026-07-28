"""The backend loads the SAME backend-contract golden fixtures the VolView
client validates. Pure-stdlib load coverage — no Girder/Mongo needed. The
neutral contract carries only synthetic task-spec fixtures; backend-specific
source formats (e.g. Slicer XML) and their translated goldens are backend test
fixtures, exercised by ``test_slicer_spec_translation``.
"""

import copy
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
    # Both staged types share ONE descriptor shape; only the tag differs, which
    # is what keeps the backend's staging transport type-blind.
    assert wire["stage-input.annotations"]["type"] == "annotations"
    assert wire["stage-input.annotations"]["referenceImage"]["type"] == "image"
    assert set(wire["stage-input.annotations"]) == set(wire["stage-input.labelmap"])
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
        "stage-input-unknown-type",
        "annotations-bad-schema-version",
        "annotations-bad-space",
        "annotations-two-point-polygon",
        "annotations-dangling-label",
        "annotations-session-field",
        "annotations-zero-normal",
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
    assert "annotations-file" in names
    task_spec_schema = contract_loader.load_generated_schema("task-spec")
    assert task_spec_schema["type"] == "object"


def _stage_input_validator():
    """A validator for the staged-descriptor schema.

    The normative ``stageInputDescriptorSchema`` is a discriminated union, so the
    generated artifact is a ``oneOf`` -- one branch per member of the backend's
    ``inputs._STAGEABLE_TYPES``, not a single object. Both branches must accept
    their fixture, and a type outside the union must match NO branch.
    """
    schema = contract_loader.load_generated_schema("stage-input-descriptor")
    assert set(schema) == {"$schema", "oneOf"}
    assert len(schema["oneOf"]) == 2
    return jsonschema.Draft202012Validator(schema)


def test_stage_input_descriptor_schema_accepts_both_staged_types():
    validator = _stage_input_validator()
    validator.validate(contract_loader.load_fixture("wire/stage-input.labelmap.json"))
    validator.validate(
        contract_loader.load_fixture("wire/stage-input.annotations.json")
    )


def test_stage_input_descriptor_schema_rejects_an_unknown_type():
    # Mirrors the backend's own 400 (inputs.validateStagedDescriptor): the type
    # discriminator is closed, so an unlisted staged type fails closed on both
    # sides of the boundary.
    validator = _stage_input_validator()
    unknown = contract_loader.load_fixture("negative/stage-input-unknown-type.json")
    assert not validator.is_valid(unknown)


def test_annotations_file_fixture_validates_and_negatives_fail_closed():
    # The annotations wire file is the interchange format a CLI reads and writes;
    # the backend never parses it, but it ships the schema, so the golden and
    # every negative are pinned here.
    validator = jsonschema.Draft202012Validator(
        contract_loader.load_generated_schema("annotations-file")
    )
    golden = contract_loader.load_fixture("wire/annotations-file.json")
    validator.validate(golden)
    assert golden["schemaVersion"] == 1
    assert golden["space"] == "LPS"
    # Per-kind label namespaces: the SAME label name legally carries different
    # styles in different tool kinds, which is why labels are not one flat map.
    assert (
        golden["labels"]["rulers"]["lesion"] != golden["labels"]["rectangles"]["lesion"]
    )
    assert set(golden["tools"]) == {"rulers", "rectangles", "polygons"}

    for stem in (
        "annotations-bad-schema-version",
        "annotations-bad-space",
        "annotations-two-point-polygon",
        "annotations-session-field",
    ):
        bad = contract_loader.load_fixture("negative/%s.json" % stem)
        assert not validator.is_valid(bad), stem


def test_dangling_label_negative_is_a_semantic_not_structural_rejection():
    # Label-reference integrity is a cross-field rule the generated JSON Schema
    # cannot express, exactly like the task-spec constraint pass: the structural
    # schema accepts it and the decoder's semantic pass rejects it.
    validator = jsonschema.Draft202012Validator(
        contract_loader.load_generated_schema("annotations-file")
    )
    dangling = contract_loader.load_fixture("negative/annotations-dangling-label.json")
    validator.validate(dangling)
    labelled = [
        tool
        for tools in dangling["tools"].values()
        for tool in tools
        if tool.get("labelName")
    ]
    assert labelled
    assert any(
        tool["labelName"] not in (dangling.get("labels") or {}).get(kind, {})
        for kind, tools in dangling["tools"].items()
        for tool in tools
        if tool.get("labelName")
    )


def test_zero_normal_negative_is_a_semantic_not_structural_rejection():
    validator = jsonschema.Draft202012Validator(
        contract_loader.load_generated_schema("annotations-file")
    )
    zero_normal = contract_loader.load_fixture(
        "negative/annotations-zero-normal.json"
    )
    validator.validate(zero_normal)
    assert zero_normal["tools"]["rulers"][0]["frameOfReference"][
        "planeNormal"
    ] == [0, 0, 0]


def test_annotations_schema_rejects_reserved_record_keys():
    validator = jsonschema.Draft202012Validator(
        contract_loader.load_generated_schema("annotations-file")
    )
    golden = contract_loader.load_fixture("wire/annotations-file.json")

    unsafe_labels = copy.deepcopy(golden)
    unsafe_labels["labels"]["rulers"]["__proto__"] = {}
    assert not validator.is_valid(unsafe_labels)

    unsafe_metadata = copy.deepcopy(golden)
    unsafe_metadata["tools"]["rulers"][0]["metadata"] = {
        "__proto__": "value"
    }
    assert not validator.is_valid(unsafe_metadata)


def test_add_annotations_intent_is_a_known_strict_intent():
    # The typed annotations OUTPUT the backend emits in results.py: a strict
    # known-intent branch member carrying the idempotency source triple.
    schema = contract_loader.load_generated_schema("result-intent")
    strict = jsonschema.Draft202012Validator(schema["anyOf"][0])
    intent = contract_loader.load_fixture("wire/intent.add-annotations.json")
    strict.validate(intent)
    assert intent["intent"] == "add-annotations"
    assert set(intent["source"]) == {"providerId", "jobId", "outputId"}
    # No ``segments`` equivalent: labels ride inside the annotations file.
    assert "segments" not in intent
