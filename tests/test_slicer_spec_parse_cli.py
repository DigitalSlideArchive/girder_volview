"""Chunk 32: pin ``slicer_spec.parse_cli`` -- the single XML walk that replaces
the facade's two duplicate walkers (the deleted ``processing._cliCategory`` and
``processing._parseCliOutputs``).

This is the coverage the chunk mandates BEFORE those walkers are deleted: it
pins ``parse_cli(xml)["category"]`` and ``parse_cli(xml)["outputs"]`` to the
exact values the old walkers produced (category = stripped ``<category>`` text
or ``None``; outputs = ``{name, tag, isLabel, fileExtensions}`` descriptors for
``<image>``/``<file>`` params on the output channel). The real radiology CLI
XMLs are exercised alongside synthetic edge cases (whitespace, missing name,
unparseable, non-output channel) so the consolidation is proven equivalent.

Like ``test_slicer_spec_translation``, the pure-stdlib translator is loaded
straight from its file -- importing ``girder_volview`` would pull in Girder,
which this suite does not need.
"""

import importlib.util
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Load the pure-stdlib parser without importing the Girder-bound package.
# ---------------------------------------------------------------------------

_SLICER_SPEC_PATH = (
    Path(__file__).resolve().parent.parent
    / "girder_volview" / "facade" / "slicer_spec.py"
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


# ---------------------------------------------------------------------------
# category -- stripped <category> text, or None (fail-closed for D11 scoping)
# ---------------------------------------------------------------------------

def test_category_present_is_returned_stripped():
    assert parse_cli(_xml("Radiology"))["category"] == "Radiology"
    assert parse_cli(_xml(" Filtering "))["category"] == "Filtering"


def test_category_absent_or_blank_is_none():
    assert parse_cli(_xml())["category"] is None            # no <category>
    assert parse_cli(_xml(""))["category"] is None           # empty text
    assert parse_cli("")["category"] is None                 # empty document
    assert parse_cli(None)["category"] is None               # no xml at all


def test_category_unparseable_is_none_not_raised():
    result = parse_cli("<broken")
    assert result == {"category": None, "outputs": [], "params": []}


# ---------------------------------------------------------------------------
# outputs -- {name, tag, isLabel, fileExtensions} for image/file output params
# ---------------------------------------------------------------------------

def test_image_output_descriptor():
    xml = _xml("Radiology", _output_param("image", "outVol", ext=".nii.gz"))
    assert parse_cli(xml)["outputs"] == [
        {"name": "outVol", "tag": "image", "isLabel": False,
         "fileExtensions": ".nii.gz"},
    ]


def test_label_image_and_file_outputs():
    xml = _xml("Radiology",
               _output_param("image", "outSeg", type_attr="label", ext=".nrrd")
               + _output_param("file", "outLabels", ext=".JSON"))
    assert parse_cli(xml)["outputs"] == [
        {"name": "outSeg", "tag": "image", "isLabel": True,
         "fileExtensions": ".nrrd"},
        # fileExtensions is lowercased (byte-for-byte with the old walker).
        {"name": "outLabels", "tag": "file", "isLabel": False,
         "fileExtensions": ".json"},
    ]


def test_input_channel_and_nameless_outputs_skipped():
    xml = _xml("Radiology",
               _output_param("image", "inVol", channel="input")   # not output
               + _output_param("image", None))                    # no <name>
    assert parse_cli(xml)["outputs"] == []


def test_missing_file_extensions_is_empty_string():
    xml = _xml("Radiology", _output_param("file", "report"))
    assert parse_cli(xml)["outputs"] == [
        {"name": "report", "tag": "file", "isLabel": False, "fileExtensions": ""},
    ]


# ---------------------------------------------------------------------------
# Real radiology CLI XMLs -- the values the facade actually ships
# ---------------------------------------------------------------------------

_REAL_CASES = [
    ("median-filter.xml", "Radiology", [
        {"name": "outputVolume", "tag": "image", "isLabel": False,
         "fileExtensions": ".nii.gz"},
    ]),
    ("masked-median-filter.xml", "Radiology", [
        {"name": "outputVolume", "tag": "image", "isLabel": False,
         "fileExtensions": ".nii.gz"},
    ]),
    # Chunk 34: the segmentation CLIs emit a single .seg.nrrd labelmap whose
    # per-label names/colors ride inside the file -- the old `.json` sidecar
    # (`outputLabels`) is gone, so parse_cli surfaces exactly one output.
    ("otsu-segmentation.xml", "Radiology", [
        {"name": "outputLabelmap", "tag": "image", "isLabel": True,
         "fileExtensions": ".seg.nrrd"},
    ]),
    ("threshold-segmentation.xml", "Radiology", [
        {"name": "outputLabelmap", "tag": "image", "isLabel": True,
         "fileExtensions": ".seg.nrrd"},
    ]),
]


@pytest.mark.parametrize("stem,category,outputs", _REAL_CASES)
def test_real_cli_xml_category_and_outputs(stem, category, outputs):
    xml = (_CLI_XML_DIR / stem).read_text()
    parsed = parse_cli(xml)
    assert parsed["category"] == category
    assert parsed["outputs"] == outputs


def test_params_surface_is_populated_for_a_real_cli():
    # parse_cli also exposes the raw parsed params (the third consolidated walk);
    # a real CLI yields a non-empty ordered list.
    xml = (_CLI_XML_DIR / "median-filter.xml").read_text()
    params = parse_cli(xml)["params"]
    assert isinstance(params, list) and params
    assert all("tag" in p and "channel" in p for p in params)
