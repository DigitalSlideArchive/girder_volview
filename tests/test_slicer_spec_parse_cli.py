"""Pin ``slicer_spec.parse_cli``: ``parse_cli(xml)["category"]`` (stripped
``<category>`` text or ``None``) and ``parse_cli(xml)["outputs"]``
(``{name, tag, isLabel, fileExtensions}`` descriptors for ``<image>``/``<file>``
params on the output channel). The real radiology CLI XMLs are exercised
alongside synthetic edge cases (whitespace, missing name, unparseable,
non-output channel).

Like ``test_slicer_spec_translation``, the pure-stdlib translator is loaded
straight from its file -- importing ``girder_volview`` would pull in Girder,
which this suite does not need.
"""

import importlib.util
from pathlib import Path

import pytest


_SLICER_SPEC_PATH = (
    Path(__file__).resolve().parent.parent
    / "girder_volview"
    / "backend"
    / "slicer_spec.py"
)


def _load_slicer_spec():
    spec = importlib.util.spec_from_file_location(
        "slicer_spec_parse_cli_under_test", _SLICER_SPEC_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_spec = _load_slicer_spec()
parse_cli = _spec.parse_cli
_parse_float = _spec._parse_float


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.5", 1.5),
        ("  2 ", 2.0),
        ("-3.25", -3.25),
        ("1e3", 1000.0),
        # JS parseFloat leniency: read the leading numeric run, ignore the rest,
        # rather than raising ValueError and 500-ing the task-spec endpoint.
        ("1,000", 1.0),
        ("50%", 50.0),
        ("1.5x", 1.5),
    ],
)
def test_parse_float_matches_js_parsefloat_leniency(value, expected):
    assert _parse_float(value) == expected


def test_parse_float_without_a_leading_number_is_nan_not_a_raise():
    result = _parse_float("N/A")
    assert result != result  # NaN, matching JS parseFloat — no ValueError


_CLI_XML_DIR = Path(__file__).resolve().parent / "slicer_xml"


def _xml(category=None, body=""):
    """A minimal Slicer Execution Model XML with an optional ``<category>``."""
    cat = "  <category>%s</category>\n" % category if category is not None else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<executable>\n"
        "%s"
        "  <title>Tool</title>\n"
        "  <description>x</description>\n"
        "%s"
        "</executable>\n"
    ) % (cat, body)


def _output_param(tag, name, channel="output", type_attr=None, ext=None):
    type_str = ' type="%s"' % type_attr if type_attr is not None else ""
    ext_str = ' fileExtensions="%s"' % ext if ext is not None else ""
    name_el = "      <name>%s</name>\n" % name if name is not None else ""
    return (
        "  <parameters>\n"
        "    <label>Group</label>\n"
        "    <%s%s%s>\n"
        "%s"
        "      <channel>%s</channel>\n"
        "    </%s>\n"
        "  </parameters>\n"
    ) % (tag, type_str, ext_str, name_el, channel, tag)


# category falls back to None, fail-closed for task scoping.


def test_category_present_is_returned_stripped():
    assert parse_cli(_xml("Radiology"))["category"] == "Radiology"
    assert parse_cli(_xml(" Filtering "))["category"] == "Filtering"


def test_category_absent_or_blank_is_none():
    assert parse_cli(_xml())["category"] is None  # no <category>
    assert parse_cli(_xml(""))["category"] is None  # empty text
    assert parse_cli("")["category"] is None  # empty document
    assert parse_cli(None)["category"] is None  # no xml at all


def test_category_unparseable_is_none_not_raised():
    result = parse_cli("<broken")
    assert result == {"category": None, "outputs": [], "params": []}


def test_image_output_descriptor():
    xml = _xml("Radiology", _output_param("image", "outVol", ext=".nii.gz"))
    assert parse_cli(xml)["outputs"] == [
        {
            "name": "outVol",
            "tag": "image",
            "isLabel": False,
            "fileExtensions": ".nii.gz",
        },
    ]


def test_label_image_and_file_outputs():
    xml = _xml(
        "Radiology",
        _output_param("image", "outSeg", type_attr="label", ext=".nrrd")
        + _output_param("file", "outLabels", ext=".JSON"),
    )
    assert parse_cli(xml)["outputs"] == [
        {"name": "outSeg", "tag": "image", "isLabel": True, "fileExtensions": ".nrrd"},
        # fileExtensions is lowercased.
        {
            "name": "outLabels",
            "tag": "file",
            "isLabel": False,
            "fileExtensions": ".json",
        },
    ]


def test_input_channel_and_nameless_outputs_skipped():
    xml = _xml(
        "Radiology",
        _output_param("image", "inVol", channel="input")  # not output
        + _output_param("image", None),
    )  # no <name>
    assert parse_cli(xml)["outputs"] == []


def test_missing_file_extensions_is_empty_string():
    xml = _xml("Radiology", _output_param("file", "report"))
    assert parse_cli(xml)["outputs"] == [
        {"name": "report", "tag": "file", "isLabel": False, "fileExtensions": ""},
    ]


_REAL_CASES = [
    (
        "median-filter.xml",
        "Radiology",
        [
            {
                "name": "outputVolume",
                "tag": "image",
                "isLabel": False,
                "fileExtensions": ".nii.gz",
            },
        ],
    ),
    (
        "masked-median-filter.xml",
        "Radiology",
        [
            {
                "name": "outputVolume",
                "tag": "image",
                "isLabel": False,
                "fileExtensions": ".nii.gz",
            },
        ],
    ),
    # The segmentation CLIs emit a single .seg.nrrd labelmap whose per-label
    # names/colors ride inside the file, so there is exactly one output.
    (
        "otsu-segmentation.xml",
        "Radiology",
        [
            {
                "name": "outputLabelmap",
                "tag": "image",
                "isLabel": True,
                "fileExtensions": ".seg.nrrd",
            },
        ],
    ),
    (
        "threshold-segmentation.xml",
        "Radiology",
        [
            {
                "name": "outputLabelmap",
                "tag": "image",
                "isLabel": True,
                "fileExtensions": ".seg.nrrd",
            },
        ],
    ),
]


@pytest.mark.parametrize("stem,category,outputs", _REAL_CASES)
def test_real_cli_xml_category_and_outputs(stem, category, outputs):
    xml = (_CLI_XML_DIR / stem).read_text()
    parsed = parse_cli(xml)
    assert parsed["category"] == category
    assert parsed["outputs"] == outputs


def test_params_surface_is_populated_for_a_real_cli():
    xml = (_CLI_XML_DIR / "median-filter.xml").read_text()
    params = parse_cli(xml)["params"]
    assert isinstance(params, list) and params
    assert all("tag" in p and "channel" in p for p in params)


_parse_default = _spec._parse_default


@pytest.mark.parametrize(
    "widget_type",
    ["string", "number", "boolean", "string-vector", "number-vector"],
)
def test_template_default_is_skipped_for_every_widget_type(widget_type):
    # A {{template}} placeholder is deployment-side plumbing, not a value; a
    # leaked literal (e.g. "__default__" or the raw braces) would pre-fill the
    # client form and ride into a submission.
    import xml.etree.ElementTree as ET

    default_el = ET.fromstring("<default>{{some_template}}</default>")
    assert _parse_default(widget_type, default_el) is None
