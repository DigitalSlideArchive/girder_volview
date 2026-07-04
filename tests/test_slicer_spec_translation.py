"""Chunk 6 conformance: the facade's Slicer-XML -> task-spec translator must
reproduce the Chunk-5 golden fixtures exactly, and fail closed on constructs it
cannot map.

The translator (``girder_volview/facade/slicer_spec.py``) is pure standard
library, so it is loaded directly from its file here -- importing the
``girder_volview`` package would pull in Girder, which this suite (like
``contract_loader``) deliberately does not need.

Source XMLs:
- the three real radiology CLI XMLs are vendored under ``tests/slicer_xml/``
  (copied from the ``volview-radiology-cli`` repo -- they are not part of
  VolView's synced ``processing-contract`` package);
- the synthetic bounds/enum XML is read from the synced contract dir
  (``tests/contract/fixtures/task-spec-xml/``), the single source of truth for
  it.

Comparison is on *parsed* JSON (Python dict/list equality), so key order and
whitespace never fail the test -- the Chunk 6 acceptance requirement.
"""

import importlib.util
from pathlib import Path

import pytest

import contract_loader


# ---------------------------------------------------------------------------
# Load the pure-stdlib translator without importing the Girder-bound package.
# ---------------------------------------------------------------------------

_FACADE_ROOT = Path(__file__).resolve().parent.parent
_SLICER_SPEC_PATH = _FACADE_ROOT / "girder_volview" / "facade" / "slicer_spec.py"


def _load_translator():
    spec = importlib.util.spec_from_file_location(
        "slicer_spec_under_test", _SLICER_SPEC_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_translator = _load_translator()
translate_slicer_xml = _translator.translate_slicer_xml

_KNOWN_KINDS = frozenset(
    ("int", "float", "string", "bool", "enum", "sourceRef", "bounds")
)

_CLI_XML_DIR = Path(__file__).resolve().parent / "slicer_xml"
_CONTRACT_XML_DIR = contract_loader.FIXTURES_ROOT / "task-spec-xml"

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
        _CONTRACT_XML_DIR / "synthetic-bounds-enum.xml",
        "SyntheticRegionEnum",
        "synthetic-bounds-enum",
    ),
]

_CASE_IDS = [stem for _, _, stem in _CONFORMANCE_CASES]


def _task_spec_validator():
    """A JSON Schema validator for the generated task-spec schema, or skip.

    The generated schema is internal conformance tooling (D2: not the contract
    format); it is the facade-side stand-in for the normative ``zod`` schema.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = contract_loader.load_generated_schema("task-spec")
    return jsonschema.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# Positive conformance: translate(source XML) == golden fixture, and the
# translated spec is itself schema-valid (the two-sided honesty check).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("xml_path,task_id,stem", _CONFORMANCE_CASES, ids=_CASE_IDS)
def test_translation_reproduces_golden_fixture(xml_path, task_id, stem):
    spec = translate_slicer_xml(xml_path.read_text(), task_id)
    expected = contract_loader.load_fixture("task-spec/{}.json".format(stem))
    assert spec == expected


@pytest.mark.parametrize("xml_path,task_id,stem", _CONFORMANCE_CASES, ids=_CASE_IDS)
def test_translated_spec_validates_against_generated_schema(xml_path, task_id, stem):
    validator = _task_spec_validator()
    spec = translate_slicer_xml(xml_path.read_text(), task_id)
    validator.validate(spec)  # raises jsonschema.ValidationError if invalid


# ---------------------------------------------------------------------------
# The image-type -> accepts binding (WI2 / D10). The fixtures exercise the
# ``scalar``/absent -> ["image"] branch on inputs and ``label`` -> "labelmap"
# on outputs; the input ``label`` -> ["labelmap"] branch (the labelmap-input
# CLI is Chunk 16) is locked here.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# b3 injection params are dropped (Chunk 10 / D10). A CLI that fetches its own
# inputs declares ``girderApiUrl``/``girderToken`` as ``<string>`` params so
# ``slicer_cli_web`` can inject them at run time; these are server plumbing and
# must never surface as client task params. The real radiology CLI XMLs now
# carry them (the conformance cases above already prove they translate to the
# token-free golden fixtures); this locks the behavior directly and checks that
# skipping them does not perturb the remaining params' order numbers.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fail closed: an <image> type that is neither absent/scalar nor label, and any
# unmapped element tag, are emitted as an *unknown field kind* (never coerced
# into sourceRef, never dropped) so the client's schema validation rejects the
# whole spec -- the shape the Chunk-5 negative fixture pins.
# ---------------------------------------------------------------------------

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


def test_unmapped_element_tag_is_unknown_kind_and_fails_closed():
    spec = translate_slicer_xml(_UNMAPPED_TAG_XML, "UnmappedTag")
    param = spec["parameters"][0]
    assert param["kind"] == "color"
    assert param["kind"] not in _KNOWN_KINDS
    validator = _task_spec_validator()
    assert not validator.is_valid(spec)


# ---------------------------------------------------------------------------
# Negative golden fixtures (Chunk 5) -- the facade side of "fail closed".
# ---------------------------------------------------------------------------


def test_unknown_field_kind_fixture_rejected_by_schema():
    validator = _task_spec_validator()
    bad = contract_loader.load_fixture("negative/unknown-field-kind.json")
    assert not validator.is_valid(bad)


def test_constraint_violation_fixture_is_self_inconsistent():
    # The generated JSON Schema encodes each field's type but not zod's
    # cross-field refine (default <= max), so it cannot reject this fixture --
    # the client's zod suite (Chunk 5) does. Assert the inconsistency it pins,
    # so the negative's intent is still covered facade-side.
    bad = contract_loader.load_fixture("negative/constraint-violation.json")
    radius = next(p for p in bad["parameters"] if p["kind"] == "int")
    assert radius["default"] > radius["max"]


# ---------------------------------------------------------------------------
# <region> -> bounds coordinate conversion (WI3 / D8). No shipped CLI carries a
# <region> default, so the synthetic fixture pins only the structural mapping
# (bounds, no default); the RAS-center/radius -> LPS-min/max convention chosen
# for a *present* default is locked here (see slicer_spec._region_default_to_bounds).
# ---------------------------------------------------------------------------

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
