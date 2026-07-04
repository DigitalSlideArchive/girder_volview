"""The facade loads the SAME processing-contract golden fixtures the VolView
client validates (Chunk 5 acceptance: "the facade test suite loads the same
fixtures"). Pure-stdlib load coverage — no Girder/Mongo needed. The translation
conformance ASSERTIONS (facade-emitted spec == golden fixture) arrive in Chunk 6.
"""

import contract_loader


def test_task_spec_fixtures_load():
    specs = contract_loader.load_fixture_dir("task-spec")
    assert set(specs) == {
        "masked-median-filter",
        "median-filter",
        "otsu-segmentation",
        "threshold-segmentation",
        "synthetic-bounds-enum",
    }
    for spec in specs.values():
        assert spec["specVersion"] == 1
        assert spec["id"]
        assert spec["title"]
        assert isinstance(spec["parameters"], list)
        assert isinstance(spec["outputs"], list)


def test_source_ref_accepts_open_type_tags():
    median = contract_loader.load_fixture("task-spec/median-filter.json")
    source_ref = next(p for p in median["parameters"] if p["kind"] == "sourceRef")
    assert source_ref["accepts"] == ["image"]


def test_wire_fixtures_load():
    wire = contract_loader.load_fixture_dir("wire")
    assert wire["status.cancelled"]["state"] == "cancelled"
    assert wire["status.error-tail"]["errorTail"]
    assert wire["input-value.dicom-series"]["type"] == "image"
    assert len(wire["input-value.dicom-series"]["uris"]) > 1
    assert wire["input-value.labelmap"]["type"] == "labelmap"
    assert wire["job-handle"]["inputUris"]
    assert wire["job-results.missing"]["missing"] == 2
    assert wire["job-results.error"]["state"] == "failed"


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
    assert set(negatives) == {"unknown-field-kind", "constraint-violation"}


def test_generated_schemas_present_and_parse():
    names = contract_loader.list_generated_schemas()
    assert "task-spec" in names
    assert "result-intent" in names
    task_spec_schema = contract_loader.load_generated_schema("task-spec")
    assert task_spec_schema["type"] == "object"


def test_synthetic_bounds_enum_xml_present():
    xml_path = (
        contract_loader.FIXTURES_ROOT / "task-spec-xml" / "synthetic-bounds-enum.xml"
    )
    xml = xml_path.read_text()
    assert "<region>" in xml
    assert "<string-enumeration>" in xml
