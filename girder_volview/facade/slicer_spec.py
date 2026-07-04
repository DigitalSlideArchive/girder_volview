"""Slicer Execution Model XML -> VolView task spec (Seam 2, the facade half).

Chunk 6 ports the client parser's mapping tables
(``src/processing/adapters/slicer-cli/parser/`` in VolView, itself ported from
``slicer_cli_web``'s ``parser.js``) to the facade so the server emits VolView's
own ``zod``-defined task spec (decision D2) and the client never parses a
backend's XML. **Ported, not redesigned** -- the golden fixtures under
``tests/contract/fixtures/task-spec/`` pin the output exactly.

Two mappings are new here (the client port carried neither):

- input ``<image>`` ``type`` -> ``sourceRef.accepts`` (Seam 1 binding
  convention, D10): absent/``scalar`` -> ``["image"]``, ``label`` ->
  ``["labelmap"]``; anything else -> an *unknown field kind* so the client
  fails closed (Seam 2 "Unknown field kind -> fail closed").
- Slicer ``<region>`` -> the ``bounds`` field kind (axis-aligned world box,
  LPS on the wire; D8).

Pure standard library (``xml.etree``) so it imports without Girder -- the
conformance test drives it with no server/Mongo.
"""

import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# XML helpers -- direct-child element access.
#
# ElementTree iterates only child *elements* (no text/comment nodes), so a
# direct-child lookup is a simple filter -- the DOM ``firstChild``/``children``
# the TS parser walked, minus the text-node bookkeeping.
# ---------------------------------------------------------------------------


def _first_child(el, tag):
    return next((c for c in el if c.tag == tag), None)


def _all_children(el, tag):
    return [c for c in el if c.tag == tag]


def _child_text(el, tag):
    child = _first_child(el, tag)
    if child is None or child.text is None:
        return ""
    return child.text


# ---------------------------------------------------------------------------
# Ported mapping tables -- widget.ts / convert.ts / constraints.ts /
# defaultValue.ts. DO NOT redesign these: the fixtures pin them byte for byte.
# ---------------------------------------------------------------------------

# widget.ts ``TYPE_MAP``: Slicer element tag -> widget type. An unmapped tag
# yields ``None`` (the caller treats it as an unknown field kind, fail closed).
_TYPE_MAP = {
    "integer": "number",
    "float": "number",
    "double": "number",
    "boolean": "boolean",
    "string": "string",
    "integer-vector": "number-vector",
    "float-vector": "number-vector",
    "double-vector": "number-vector",
    "string-vector": "string-vector",
    "integer-enumeration": "number-enumeration",
    "float-enumeration": "number-enumeration",
    "double-enumeration": "number-enumeration",
    "string-enumeration": "string-enumeration",
    "region": "region",
    "image": "image",
    "file": "file",
    "item": "item",
    "directory": "directory",
    "multi": "multi",
}


def _widget_type(tag):
    return _TYPE_MAP.get(tag)


def _parse_float(value):
    # JS ``parseFloat``. The shipped radiology CLIs use clean numeric strings.
    return float(value)


def _convert(widget_type, value):
    """convert.ts -- coerce a raw XML string to the widget's value type."""
    if widget_type in ("number", "number-enumeration"):
        return _parse_float(value)
    if widget_type == "boolean":
        return value.lower() == "true"
    if widget_type == "number-vector":
        return [_parse_float(s) for s in value.split(",")]
    if widget_type == "string-vector":
        return value.split(",")
    return value


def _parse_constraints(widget_type, constraints_el):
    """constraints.ts -- ``<constraints>`` -> ``{min,max,step}`` (converted)."""
    if constraints_el is None:
        return {}
    spec = {}
    minimum = _child_text(constraints_el, "minimum")
    maximum = _child_text(constraints_el, "maximum")
    step = _child_text(constraints_el, "step")
    if minimum:
        spec["min"] = _convert(widget_type, minimum)
    if maximum:
        spec["max"] = _convert(widget_type, maximum)
    if step:
        spec["step"] = _convert(widget_type, step)
    return spec


def _parse_default(widget_type, default_el):
    """defaultValue.ts -- ``<default>`` -> the converted value, or ``None``.

    Template placeholders (``{{x}}``) are skipped exactly as the JS parser did.
    """
    if default_el is None:
        return None
    text = default_el.text or ""
    if len(text) == 0:
        return None
    is_template = text[:2] == "{{" and text[-2:] == "}}"
    if not is_template:
        return _convert(widget_type, text)
    defstr = "__default__"
    converted = _convert(widget_type, defstr)
    if converted == defstr:
        return converted
    return None


# ---------------------------------------------------------------------------
# Parse -- <executable> -> ordered parsed params (port of parse.ts / panel.ts /
# group.ts / param.ts). Each panel's direct-child <label> opens a group/section;
# the params that follow it (up to the next <label>, <description> excluded)
# belong to it. Output shaping is left to the translate layer below.
# ---------------------------------------------------------------------------


def _parse_param(param_el, section):
    tag = param_el.tag
    widget = _widget_type(tag)
    channel = "output" if _child_text(param_el, "channel") == "output" else "input"
    param_id = _child_text(param_el, "name") or _child_text(param_el, "longflag")
    required = len(_child_text(param_el, "index")) > 0
    values = None
    if widget in ("string-enumeration", "number-enumeration"):
        values = [
            _convert(widget, el.text or "")
            for el in _all_children(param_el, "element")
        ]
    return {
        "tag": tag,  # slicerType -- the raw element name
        "widget": widget,  # WidgetType, or None for an unmapped tag
        "channel": channel,
        "id": param_id,
        "title": _child_text(param_el, "label"),
        "help": _child_text(param_el, "description"),
        "section": section,
        "required": required,
        "imageType": param_el.get("type"),  # NEW: input <image> type -> accepts
        "fileExtensions": param_el.get("fileExtensions"),
        "values": values,
        "default": _parse_default(widget, _first_child(param_el, "default")),
        "constraints": _parse_constraints(
            widget, _first_child(param_el, "constraints")
        ),
    }


def _parse_panel(panel_el):
    """Group a ``<parameters>`` panel's children by their leading ``<label>``.

    Returns ``[(section_label, [param_el, ...]), ...]`` in document order.
    """
    groups = []
    current = None
    for child in panel_el:
        if child.tag == "label":
            current = (child.text or "", [])
            groups.append(current)
        elif child.tag == "description":
            continue
        elif current is not None:
            current[1].append(child)
    return groups


def _parse_executable(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("Invalid Slicer CLI XML: {}".format(exc))
    if root.tag != "executable":
        raise ValueError("Slicer CLI XML missing <executable>")
    params = []
    for panel_el in _all_children(root, "parameters"):
        for section, param_els in _parse_panel(panel_el):
            for param_el in param_els:
                params.append(_parse_param(param_el, section))
    return {
        "title": _child_text(root, "title"),
        "description": _child_text(root, "description"),
        "params": params,
    }


# ---------------------------------------------------------------------------
# Translate -- parsed params -> VolView task spec. This is where the imaging
# field kinds (sourceRef / bounds) and the int/float split are produced; the
# ported tables above deliberately do not know the spec vocabulary (D2).
# ---------------------------------------------------------------------------

_SPEC_VERSION = 1

# Below-the-line b3 injection params (D10): a CLI that fetches its own inputs via
# girder_client declares ``girderApiUrl``/``girderToken`` as ``<string>`` params
# so ``slicer_cli_web`` can inject them at run time (the HistomicsTK
# ``example-girder-requests`` convention). They are server-plumbing, never task
# parameters, so the translator drops them: they must not reach the client spec/
# form, and the golden task-spec fixtures carry none. Reconciles Chunk 10's b3
# token injection against the frozen Chunk-5 keystone (WORKORDER Chunk 10 pin;
# the submit-time reserved-param deny-list is a separate defense, Chunk 21).
_RESERVED_INPUT_PARAMS = frozenset(("girderApiUrl", "girderToken"))

# slicerType (element tag) -> scalar spec kind. Recovers the int/float split the
# ported widget table collapses to a single "number".
_SCALAR_KIND = {
    "integer": "int",
    "float": "float",
    "double": "float",
    "string": "string",
    "boolean": "bool",
}

_ENUM_TAGS = frozenset(
    ("integer-enumeration", "float-enumeration", "double-enumeration",
     "string-enumeration")
)


def _json_number(value):
    """Canonicalize an integral float to ``int`` so emitted JSON matches the
    fixtures (``50.0`` -> ``50``); genuine fractionals pass through."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _image_accepts(image_type):
    """input ``<image>`` ``type`` -> ``sourceRef.accepts`` (Seam 1 / D10).

    ``None`` (no type attr) or ``scalar`` -> ``["image"]``; ``label`` ->
    ``["labelmap"]``; anything else -> ``None``, signalling the caller to emit
    an unknown field kind (fail closed). The tag set stays an open vocabulary
    -- no closed server enum.
    """
    if image_type is None or image_type == "scalar":
        return ["image"]
    if image_type == "label":
        return ["labelmap"]
    return None


def _output_type(parsed):
    """Declared output ``type``: image outputs reuse the accepts vocabulary
    (image/labelmap); file outputs are ``file``; anything else passes through
    (outputs are an open vocabulary -- an unknown one degrades to download)."""
    if parsed["tag"] == "file":
        return "file"
    image_type = parsed["imageType"]
    if image_type is None or image_type == "scalar":
        return "image"
    if image_type == "label":
        return "labelmap"
    return image_type


def _region_default_to_bounds(default_value):
    """Slicer ``<region>`` default -> VolView ``bounds`` (D8, WI3).

    A Slicer region default is ``cx,cy,cz,rx,ry,rz`` -- center + radius in RAS
    (Slicer's native frame). VolView ``bounds`` is an axis-aligned min/max box
    ``[xmin,xmax,ymin,ymax,zmin,zmax]`` in LPS, so RAS->LPS negates X and Y
    (swapping their min/max). Fail closed: a default that is not six parseable
    numbers yields *no* bounds default rather than a malformed one.

    No shipped radiology CLI carries a ``<region>`` default, so this coordinate
    conversion is unpinned by the golden fixtures -- the chosen convention is
    locked by ``test_slicer_spec_translation.py`` and logged in Appendix C.
    """
    if not isinstance(default_value, str) or not default_value:
        return None
    parts = [p.strip() for p in default_value.split(",")]
    if len(parts) != 6:
        return None
    try:
        cx, cy, cz, rx, ry, rz = (float(p) for p in parts)
    except ValueError:
        return None
    rx, ry, rz = abs(rx), abs(ry), abs(rz)
    x_lps = sorted((-(cx - rx), -(cx + rx)))
    y_lps = sorted((-(cy - ry), -(cy + ry)))
    z_lps = (cz - rz, cz + rz)
    return [
        _json_number(x_lps[0]), _json_number(x_lps[1]),
        _json_number(y_lps[0]), _json_number(y_lps[1]),
        _json_number(z_lps[0]), _json_number(z_lps[1]),
    ]


def _base_fields(parsed, order):
    """The UI/identity fields every param kind shares, blanks omitted so the
    output matches the fixtures (which carry no empty strings)."""
    base = {"id": parsed["id"]}
    if parsed["title"]:
        base["title"] = parsed["title"]
    if parsed["help"]:
        base["help"] = parsed["help"]
    if parsed["section"]:
        base["section"] = parsed["section"]
    base["order"] = order
    if parsed["required"]:
        base["required"] = True
    return base


def _translate_scalar(kind, parsed, base):
    param = {"kind": kind, **base}
    constraints = parsed["constraints"]
    default = parsed["default"]
    if kind == "int":
        for key in ("min", "max", "step"):
            if key in constraints:
                param[key] = int(constraints[key])
        if default is not None:
            param["default"] = int(default)
    elif kind == "float":
        for key in ("min", "max", "step"):
            if key in constraints:
                param[key] = _json_number(constraints[key])
        if default is not None:
            param["default"] = _json_number(default)
    elif default is not None:  # string, bool
        param["default"] = default
    return param


def _translate_param(parsed, order):
    tag = parsed["tag"]
    base = _base_fields(parsed, order)

    # imaging-native field kinds -----------------------------------------
    if tag == "image":
        accepts = _image_accepts(parsed["imageType"])
        if accepts is None:
            # unknown <image> type -> unknown field kind (fail closed, D10).
            return {"kind": parsed["imageType"], **base}
        return {"kind": "sourceRef", **base, "accepts": accepts}
    if tag == "region":
        param = {"kind": "bounds", **base}
        bounds_default = _region_default_to_bounds(parsed["default"])
        if bounds_default is not None:
            param["default"] = bounds_default
        return param

    # typed scalar fields ------------------------------------------------
    if tag in _ENUM_TAGS:
        param = {"kind": "enum", **base, "options": parsed["values"] or []}
        if parsed["default"] is not None:
            param["default"] = parsed["default"]
        return param
    if tag in _SCALAR_KIND:
        return _translate_scalar(_SCALAR_KIND[tag], parsed, base)

    # anything else -> unknown field kind (fail closed) ------------------
    return {"kind": tag, **base}


def _translate_output(parsed):
    entry = {"id": parsed["id"]}
    if parsed["title"]:
        entry["title"] = parsed["title"]
    if parsed["help"]:
        entry["help"] = parsed["help"]
    entry["type"] = _output_type(parsed)
    if parsed["fileExtensions"]:
        entry["format"] = parsed["fileExtensions"]
    return entry


def translate_slicer_xml(xml_text, task_id):
    """Translate a Slicer Execution Model XML document into a VolView task spec.

    ``task_id`` is the CLI identity that becomes the spec ``id`` -- it is not
    carried in the XML, so the caller (the ``tasks/{id}/spec`` route) supplies
    it. Returns a plain ``dict`` ready to serialize as the spec JSON.
    """
    doc = _parse_executable(xml_text)
    parameters = []
    outputs = []
    order = 0
    for parsed in doc["params"]:
        if parsed["channel"] == "output":
            # File/image outputs are declarations; scalar parameter-outputs are
            # not v1 spec fields (the ported parser dropped them too).
            if parsed["tag"] in ("image", "file"):
                outputs.append(_translate_output(parsed))
            continue
        if parsed["id"] in _RESERVED_INPUT_PARAMS:
            # b3 injection plumbing (girderApiUrl/girderToken) -- below the line,
            # never a client-facing param. Skip before ``order`` advances so the
            # remaining params keep their fixture-pinned order numbers.
            continue
        parameters.append(_translate_param(parsed, order))
        order += 1

    spec = {"specVersion": _SPEC_VERSION, "id": task_id, "title": doc["title"]}
    if doc["description"]:
        spec["description"] = doc["description"]
    spec["parameters"] = parameters
    spec["outputs"] = outputs
    return spec
