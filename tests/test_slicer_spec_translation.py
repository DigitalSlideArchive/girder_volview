"""Conformance: the backend's Slicer-XML -> task-spec translator must reproduce
its golden fixtures exactly, and fail closed on constructs it cannot map.

The translator (``girder_volview/backend/slicer_spec.py``) is pure standard
library, so it is loaded directly from its file here -- importing the
``girder_volview`` package would pull in Girder, which this suite does not need.

Sources live under ``tests/slicer_xml/`` and golden translated specs under
``tests/slicer_xml/expected/``: Slicer XML is a backend concern, so the neutral
``backend-contract`` package carries no CLI-specific fixtures -- only the
generated task-spec *schema* comes from the synced contract dir.

Comparison is on *parsed* JSON (Python dict/list equality), so key order and
whitespace never fail the test.
"""

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

import contract_loader


_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_SLICER_SPEC_PATH = _BACKEND_ROOT / "girder_volview" / "backend" / "slicer_spec.py"


def _load_translator():
    spec = importlib.util.spec_from_file_location(
        "slicer_spec_under_test", _SLICER_SPEC_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_translator = _load_translator()
translate_slicer_xml = _translator.translate_slicer_xml
task_spec_semantic_issues = _translator.task_spec_semantic_issues
validate_task_spec = _translator.validate_task_spec

_KNOWN_KINDS = frozenset(
    ("int", "float", "string", "bool", "enum", "sourceRef", "bounds")
)

_CLI_XML_DIR = Path(__file__).resolve().parent / "slicer_xml"
_EXPECTED_DIR = _CLI_XML_DIR / "expected"


def _load_expected(stem):
    return json.loads((_EXPECTED_DIR / "{}.json".format(stem)).read_text())


# (source XML path, CLI identity supplied as the spec id, golden fixture stem)
_CONFORMANCE_CASES = [
    (_CLI_XML_DIR / "median-filter.xml", "MedianFilter", "median-filter"),
    (_CLI_XML_DIR / "otsu-segmentation.xml", "OtsuSegmentation", "otsu-segmentation"),
    (
        _CLI_XML_DIR / "threshold-segmentation.xml",
        "ThresholdSegmentation",
        "threshold-segmentation",
    ),
    (
        _CLI_XML_DIR / "masked-median-filter.xml",
        "MaskedMedianFilter",
        "masked-median-filter",
    ),
    (
        _CLI_XML_DIR / "synthetic-bounds-enum.xml",
        "SyntheticRegionEnum",
        "synthetic-bounds-enum",
    ),
]

_CASE_IDS = [stem for _, _, stem in _CONFORMANCE_CASES]


def _task_spec_validator():
    """A JSON Schema validator for the generated task-spec schema.

    The generated schema is the backend-side stand-in for the normative ``zod``
    schema. ``jsonschema`` is a hard test dep: a missing validator FAILS this
    conformance layer, never silently skips it.
    """
    schema = contract_loader.load_generated_schema("task-spec")
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize("xml_path,task_id,stem", _CONFORMANCE_CASES, ids=_CASE_IDS)
def test_translation_reproduces_golden_fixture(xml_path, task_id, stem):
    spec = translate_slicer_xml(xml_path.read_text(), task_id)
    expected = _load_expected(stem)
    assert spec == expected


@pytest.mark.parametrize("xml_path,task_id,stem", _CONFORMANCE_CASES, ids=_CASE_IDS)
def test_translated_spec_validates_against_generated_schema(xml_path, task_id, stem):
    validator = _task_spec_validator()
    spec = translate_slicer_xml(xml_path.read_text(), task_id)
    validator.validate(spec)  # raises jsonschema.ValidationError if invalid


@pytest.mark.parametrize("xml_path,task_id,stem", _CONFORMANCE_CASES, ids=_CASE_IDS)
def test_translated_spec_passes_backend_validation(xml_path, task_id, stem):
    spec = translate_slicer_xml(xml_path.read_text(), task_id)
    assert validate_task_spec(spec) is spec


# The fixtures exercise the ``scalar``/absent -> ["image"] branch on inputs and
# ``label`` -> "labelmap" on outputs; the input ``label`` -> ["labelmap"] branch
# (the labelmap-input CLI) is locked here.
_LABELMAP_INPUT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<executable>
  <title>Labelmap Input</title>
  <description>d</description>
  <parameters>
    <label>IO</label>
    <image type="label">
      <name>priorSeg</name>
      <label>Prior Segmentation</label>
      <channel>input</channel>
      <description>An existing labelmap.</description>
      <index>0</index>
    </image>
  </parameters>
</executable>
"""


def test_input_image_label_type_accepts_labelmap():
    spec = translate_slicer_xml(_LABELMAP_INPUT_XML, "LabelmapInput")
    source_ref = next(p for p in spec["parameters"] if p["kind"] == "sourceRef")
    assert source_ref["accepts"] == ["labelmap"]


# A CLI that fetches its own inputs declares ``girderApiUrl``/``girderToken`` as
# ``<string>`` params so ``slicer_cli_web`` can inject them at run time; these are
# server plumbing and must never surface as client task params. Skipping them must
# also not perturb the remaining params' order numbers.
_GIRDER_TOKEN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<executable>
  <title>Token Params</title>
  <description>d</description>
  <parameters>
    <label>IO</label>
    <image>
      <name>inputVolume</name>
      <label>Input</label>
      <channel>input</channel>
      <index>0</index>
    </image>
    <integer>
      <name>radius</name>
      <label>Radius</label>
      <longflag>radius</longflag>
      <default>1</default>
    </integer>
  </parameters>
  <parameters advanced="true">
    <label>Girder API</label>
    <string>
      <name>girderApiUrl</name>
      <longflag>girderApiUrl</longflag>
      <label>Girder API URL</label>
      <default></default>
    </string>
    <string>
      <name>girderToken</name>
      <longflag>girderToken</longflag>
      <label>Girder Token</label>
      <default></default>
    </string>
  </parameters>
</executable>
"""


def test_girder_injection_params_are_dropped_from_spec():
    spec = translate_slicer_xml(_GIRDER_TOKEN_XML, "TokenParams")
    ids = [p["id"] for p in spec["parameters"]]
    assert ids == ["inputVolume", "radius"]  # token params skipped
    assert "girderApiUrl" not in ids and "girderToken" not in ids


def test_dropping_token_params_preserves_order_numbers():
    spec = translate_slicer_xml(_GIRDER_TOKEN_XML, "TokenParams")
    by_id = {p["id"]: p for p in spec["parameters"]}
    # radius keeps order 1 even though two skipped params sit between IO and it;
    # the skip must not consume order numbers.
    assert by_id["inputVolume"]["order"] == 0
    assert by_id["radius"]["order"] == 1


# An <image> type that is neither absent/scalar nor label, and any unmapped
# element tag, are emitted as an *unknown field kind* (never coerced into
# sourceRef, never dropped) so the client's schema validation rejects the whole
# spec -- the shape the negative fixture pins.
_UNKNOWN_IMAGE_TYPE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<executable>
  <title>Unknown Image Type</title>
  <description>d</description>
  <parameters>
    <label>IO</label>
    <image type="vector">
      <name>weirdInput</name>
      <label>Weird Input</label>
      <channel>input</channel>
      <description>An image type the binding convention does not know.</description>
      <index>0</index>
    </image>
  </parameters>
</executable>
"""

_UNMAPPED_TAG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<executable>
  <title>Unmapped Tag</title>
  <description>d</description>
  <parameters>
    <label>Options</label>
    <color>
      <name>tint</name>
      <label>Tint</label>
      <channel>input</channel>
      <description>A widget kind the parser does not map.</description>
      <default>#ff0000</default>
    </color>
  </parameters>
</executable>
"""


def test_unknown_image_type_is_unknown_kind_not_source_ref():
    spec = translate_slicer_xml(_UNKNOWN_IMAGE_TYPE_XML, "UnknownImageType")
    param = next(p for p in spec["parameters"] if p["id"] == "weirdInput")
    assert param["kind"] != "sourceRef"
    assert param["kind"] not in _KNOWN_KINDS
    assert "accepts" not in param  # not coerced into a known accepts


def test_unknown_image_type_fails_closed_against_schema():
    validator = _task_spec_validator()
    spec = translate_slicer_xml(_UNKNOWN_IMAGE_TYPE_XML, "UnknownImageType")
    assert not validator.is_valid(spec)
    with pytest.raises(ValueError, match="unknown parameter kind"):
        validate_task_spec(spec)


def test_unmapped_element_tag_is_unknown_kind_and_fails_closed():
    spec = translate_slicer_xml(_UNMAPPED_TAG_XML, "UnmappedTag")
    param = spec["parameters"][0]
    assert param["kind"] == "color"
    assert param["kind"] not in _KNOWN_KINDS
    validator = _task_spec_validator()
    assert not validator.is_valid(spec)
    with pytest.raises(ValueError, match="unknown parameter kind"):
        validate_task_spec(spec)


def test_unknown_field_kind_fixture_rejected_by_schema():
    validator = _task_spec_validator()
    bad = contract_loader.load_fixture("negative/unknown-field-kind.json")
    assert not validator.is_valid(bad)


def test_constraint_violation_fixture_fails_backend_semantic_validation():
    # The generated schema cannot compare sibling fields; the required second
    # backend pass rejects the same fixture as VolView's zod refinement.
    bad = contract_loader.load_fixture("negative/constraint-violation.json")
    assert task_spec_semantic_issues(bad) == [
        {
            "path": ["parameters", 1, "default"],
            "message": "default must be <= max",
        }
    ]
    with pytest.raises(ValueError, match="default must be <= max"):
        validate_task_spec(bad)


def test_backend_semantic_validation_rejects_every_cross_field_rule():
    valid = contract_loader.load_fixture("task-spec/synthetic-all-kinds.json")
    cases = []

    bad_range = json.loads(json.dumps(valid))
    numeric = next(p for p in bad_range["parameters"] if p["kind"] == "int")
    numeric["min"], numeric["max"] = 10, 1
    cases.append((bad_range, "min must be <= max"))

    bad_step = json.loads(json.dumps(valid))
    next(p for p in bad_step["parameters"] if p["kind"] == "int")["step"] = 0
    cases.append((bad_step, "step must be > 0"))

    bad_enum = contract_loader.load_fixture("task-spec/synthetic-bounds-enum.json")
    enum = next(p for p in bad_enum["parameters"] if p["kind"] == "enum")
    enum["default"] = "not-an-option"
    cases.append((bad_enum, "default must be one of the enum options"))

    duplicate = json.loads(json.dumps(valid))
    duplicate["outputs"].append(dict(duplicate["outputs"][0]))
    cases.append((duplicate, "duplicate output id"))

    for bad, message in cases:
        with pytest.raises(ValueError, match=message):
            validate_task_spec(bad)


# No shipped CLI carries a <region> default, so the synthetic fixture pins only
# the structural mapping (bounds, no default); the RAS-center/radius ->
# LPS-min/max convention chosen for a *present* default is locked here
# (see slicer_spec._region_default_to_bounds).
_REGION_DEFAULT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<executable>
  <title>Region Default</title>
  <description>d</description>
  <parameters>
    <label>Region</label>
    <region>
      <name>roi</name>
      <label>ROI</label>
      <channel>input</channel>
      <description>World box.</description>
      <default>10,20,30,1,2,3</default>
    </region>
  </parameters>
</executable>
"""


def test_region_default_converts_ras_center_radius_to_lps_bounds():
    spec = translate_slicer_xml(_REGION_DEFAULT_XML, "RegionDefault")
    roi = next(p for p in spec["parameters"] if p["kind"] == "bounds")
    # center (10,20,30) RAS + radius (1,2,3) -> RAS x[9,11] y[18,22] z[27,33];
    # RAS->LPS negates X and Y (min/max swap), Z unchanged.
    assert roi["default"] == [-11, -9, -22, -18, 27, 33]


def test_malformed_region_default_is_omitted_fail_closed():
    xml = _REGION_DEFAULT_XML.replace("10,20,30,1,2,3", "not,a,valid,box")
    spec = translate_slicer_xml(xml, "RegionDefault")
    roi = next(p for p in spec["parameters"] if p["kind"] == "bounds")
    assert "default" not in roi
    validator = _task_spec_validator()
    validator.validate(spec)  # still a valid spec -- only the default dropped


# The client mints a crop box as an LPS min/max box, but a Slicer <region> CLI
# param expects RAS center+radius. The submit boundary inverts
# _region_default_to_bounds; this pins that inverse (round-trip identity) and its
# fail-closed behavior. Applied to a live submission in test_submit_param_guards.py.
_bounds_to_region = _translator._bounds_to_region
_region_default_to_bounds = _translator._region_default_to_bounds


def test_bounds_to_region_inverts_region_default_to_bounds():
    # region default (RAS center+radius) -> bounds (LPS box) -> region recovers
    # the original center+radius exactly; the two maps are a matched pair.
    bounds = _region_default_to_bounds("10,20,30,1,2,3")
    assert bounds == [-11, -9, -22, -18, 27, 33]
    assert _bounds_to_region(bounds) == [10, 20, 30, 1, 2, 3]


def test_bounds_to_region_round_trips_a_fractional_box():
    bounds = _region_default_to_bounds("1.5,-2.5,4.0,0.5,1.25,2.0")
    recovered = _bounds_to_region(bounds)
    assert recovered == pytest.approx([1.5, -2.5, 4.0, 0.5, 1.25, 2.0])


def test_bounds_to_region_fails_closed_on_non_six_finite():
    assert _bounds_to_region([1, 2, 3, 4, 5]) is None  # too few
    assert _bounds_to_region([1, 2, 3, 4, 5, 6, 7]) is None  # too many
    assert _bounds_to_region("not-a-list") is None
    assert _bounds_to_region([1, 2, 3, 4, 5, "x"]) is None  # non-numeric element
    assert _bounds_to_region([1, 2, 3, 4, 5, float("inf")]) is None  # non-finite
    assert _bounds_to_region([1, 2, 3, 4, 5, float("nan")]) is None


# _parse_float deliberately degrades an unparseable string to NaN (parseFloat
# parity); the translate boundary must then drop the value as absent -- int(nan)
# raises and Girder's JSON encoder rejects NaN (allow_nan=False), so a NaN
# reaching the spec 500s the whole task-spec endpoint instead of degrading one
# field.
_MESSY_NUMERIC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<executable>
  <title>Messy Numerics</title>
  <description>d</description>
  <parameters>
    <label>Params</label>
    <integer>
      <name>iterations</name>
      <label>Iterations</label>
      <description>n</description>
      <default>auto</default>
      <constraints>
        <minimum>auto</minimum>
        <maximum>10</maximum>
        <step>1</step>
      </constraints>
    </integer>
    <double>
      <name>sigma</name>
      <label>Sigma</label>
      <description>s</description>
      <default>high</default>
    </double>
    <integer-enumeration>
      <name>levels</name>
      <label>Levels</label>
      <description>l</description>
      <default>auto</default>
      <element>1</element>
      <element>auto</element>
      <element>3</element>
    </integer-enumeration>
  </parameters>
</executable>
"""


def test_messy_numeric_defaults_degrade_instead_of_breaking_the_spec():
    spec = translate_slicer_xml(_MESSY_NUMERIC_XML, "MessyNumerics")
    params = {p["id"]: p for p in spec["parameters"]}

    # int: NaN default and NaN min dropped; parseable constraints survive.
    assert "default" not in params["iterations"]
    assert "min" not in params["iterations"]
    assert params["iterations"]["max"] == 10
    assert params["iterations"]["step"] == 1

    # float: NaN default dropped.
    assert "default" not in params["sigma"]

    # numeric enum: the NaN member and NaN default drop; real members survive.
    assert params["levels"]["options"] == [1, 3]
    assert "default" not in params["levels"]

    # The degraded spec is still schema-valid -- fail soft, not open.
    _task_spec_validator().validate(spec)


def test_non_finite_region_default_is_omitted_fail_closed():
    # float("nan") PARSES, so the six-parseable-numbers guard alone lets a NaN
    # box through to the JSON encoder; non-finite components must also drop.
    xml = _REGION_DEFAULT_XML.replace("10,20,30,1,2,3", "nan,20,30,1,2,3")
    spec = translate_slicer_xml(xml, "RegionDefault")
    roi = next(p for p in spec["parameters"] if p["kind"] == "bounds")
    assert "default" not in roi
