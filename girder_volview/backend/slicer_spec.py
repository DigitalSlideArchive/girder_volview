"""Slicer Execution Model XML -> VolView task spec.

The server emits VolView's ``zod``-defined task spec so the client never parses
a backend's XML. VolView's ``backend-contract`` ``task-spec`` golden fixtures
pin the output exactly.

Pure standard library (``xml.etree``) so it imports without Girder.
"""

import math
import re
import xml.etree.ElementTree as ET


def _first_child(el, tag):
    return next((c for c in el if c.tag == tag), None)


def _all_children(el, tag):
    return [c for c in el if c.tag == tag]


def _child_text(el, tag):
    child = _first_child(el, tag)
    if child is None or child.text is None:
        return ""
    return child.text


# Mapping tables. DO NOT redesign these: the fixtures pin them byte for byte.

# Slicer element tag -> widget type. An unmapped tag yields ``None`` (the caller
# treats it as an unknown field kind, fail closed).
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


# Leading numeric run, JS ``parseFloat``-style (optional sign, digits, decimal,
# exponent).
_LEADING_FLOAT = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _parse_float(value):
    # Emulate JS ``parseFloat``: a lenient value (``"1,000"``, ``"50%"``,
    # ``"1.5x"``) degrades to its leading number rather than raising
    # ``ValueError`` and 500-ing the whole task-spec endpoint. No leading number
    # yields NaN.
    try:
        return float(value)
    except (TypeError, ValueError):
        match = _LEADING_FLOAT.match(str(value).strip())
        return float(match.group(0)) if match else float("nan")


def _convert(widget_type, value):
    """Coerce a raw XML string to the widget's value type."""
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
    """``<constraints>`` -> ``{min,max,step}`` (converted)."""
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
    """``<default>`` -> the converted value, or ``None``.

    Template placeholders (``{{x}}``) are skipped.
    """
    if default_el is None:
        return None
    text = default_el.text or ""
    if len(text) == 0:
        return None
    is_template = text[:2] == "{{" and text[-2:] == "}}"
    if is_template:
        return None
    return _convert(widget_type, text)


def _parse_param(param_el, section):
    tag = param_el.tag
    widget = _widget_type(tag)
    channel = "output" if _child_text(param_el, "channel") == "output" else "input"
    # ctk_cli identifies a <name>-less param by its longflag with leading dashes
    # stripped; slicer_cli_web binds submitted args by that identifier, so the
    # id must match or the submitted value is silently ignored.
    param_id = (
        _child_text(param_el, "name") or _child_text(param_el, "longflag").lstrip("-")
    ).strip()
    required = len(_child_text(param_el, "index")) > 0
    values = None
    if widget in ("string-enumeration", "number-enumeration"):
        values = [
            _convert(widget, el.text or "") for el in _all_children(param_el, "element")
        ]
    return {
        "tag": tag,  # the raw Slicer element name
        "widget": widget,  # WidgetType, or None for an unmapped tag
        "channel": channel,
        "id": param_id,
        "title": _child_text(param_el, "label"),
        "help": _child_text(param_el, "description"),
        "section": section,
        "required": required,
        "imageType": param_el.get("type"),  # input <image> type -> accepts
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
    Params before the first ``<label>`` (legal per the Execution Model) get an
    empty section rather than being dropped — every surface derived from this
    walk (spec, outputs, declared submit keys) must see the same param set.
    """
    groups = []
    current = None
    for child in panel_el:
        if child.tag == "label":
            current = (child.text or "", [])
            groups.append(current)
        elif child.tag == "description":
            continue
        else:
            if current is None:
                current = ("", [])
                groups.append(current)
            current[1].append(child)
    return groups


def _params_from_root(root):
    """Ordered parsed params across every ``<parameters>`` panel.

    The ONE param walk: the strict spec path (``_parse_executable``), the
    tolerant backend surface (``parse_cli``, outputs included), and the submit
    declaration map (``declared_params``) all project from it.
    """
    params = []
    for panel_el in _all_children(root, "parameters"):
        for section, param_els in _parse_panel(panel_el):
            for param_el in param_els:
                params.append(_parse_param(param_el, section))
    return params


def _parse_executable(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("Invalid Slicer CLI XML: {}".format(exc)) from exc
    if root.tag != "executable":
        raise ValueError("Slicer CLI XML missing <executable>")
    return {
        "title": _child_text(root, "title"),
        "description": _child_text(root, "description"),
        "params": _params_from_root(root),
    }


def parse_cli(xml_text):
    """Parse a Slicer CLI XML once into ``{category, outputs, params}``.

    ``category`` (the stripped ``<category>``, or ``None``) is what task scoping
    reads; ``outputs`` is the ``{name, tag, isLabel, fileExtensions}`` descriptor
    list -- every identified ``<image>``/``<file>`` output-channel param -- that
    reference-bound collection records and autofill read (``isLabel`` =
    ``type == "label"``; ``fileExtensions`` lowercased); ``params`` is the raw
    parsed-param list.

    Tolerant: an unparseable document yields
    ``{category: None, outputs: [], params: []}`` (a malformed CLI is out of
    scope / autofills nothing). The strict spec path uses ``_parse_executable``.
    """
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return {"category": None, "outputs": [], "params": []}
    category_el = root.find("category")
    if category_el is not None and category_el.text:
        category = category_el.text.strip() or None
    else:
        category = None
    params = _params_from_root(root)
    outputs = [
        {
            "name": parsed["id"],
            "tag": parsed["tag"],
            "isLabel": parsed["imageType"] == "label",
            "fileExtensions": (parsed["fileExtensions"] or "").lower(),
        }
        for parsed in params
        if parsed["channel"] == "output"
        and parsed["tag"] in ("image", "file")
        and parsed["id"]
    ]
    return {"category": category, "outputs": outputs, "params": params}


def declared_params(xml_text):
    """Every parameter a CLI declares, keyed by name, independent of UI sectioning.

    The submit boundary validates submissions against exactly what the CLI
    declares, projected from the SAME ``_params_from_root`` walk the ordered
    spec path and ``parse_cli`` outputs use, so the surfaces never drift. A
    param is any panel child whose tag is a known widget type (``_TYPE_MAP``);
    its key is its ``<name>`` (or dash-stripped ``<longflag>``), independent of
    UI sectioning.

    Each entry carries what submit-time value validation needs: ``tag`` (the raw
    Slicer element), ``widget`` (its ``_TYPE_MAP`` type), ``channel``,
    ``constraints`` (``{min,max,step}``), ``options`` (converted enumeration
    members, or ``None``) and ``required`` (the param is indexed). Tolerant: an
    unparseable document declares nothing.
    """
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return {}
    params = {}
    for parsed in _params_from_root(root):
        if parsed["widget"] is None:
            continue
        name = parsed["id"]
        if not name:
            continue
        params[name] = {
                "tag": parsed["tag"],
                "widget": parsed["widget"],
                "channel": parsed["channel"],
                "constraints": parsed["constraints"],
                # ``_parse_param`` fills ``values`` only for enum widgets.
                "options": parsed["values"],
                # Indexed (positional) params are required on the CLI command
                # line; the submit boundary enforces presence for inputs.
                "required": parsed["required"],
            }
    return params


_SPEC_VERSION = 1
_PARAMETER_KINDS = frozenset(
    ("int", "float", "string", "bool", "enum", "sourceRef", "bounds")
)

# A CLI that fetches its own inputs via girder_client declares
# ``girderApiUrl``/``girderToken`` as ``<string>`` params so ``slicer_cli_web``
# can inject them at run time (the HistomicsTK ``example-girder-requests``
# convention). They are server plumbing, never task parameters, so the translator
# drops them: they must not reach the client spec/form. (The submit-time
# reserved-param deny-list is a separate defense.)
_RESERVED_INPUT_PARAMS = frozenset(("girderApiUrl", "girderToken"))

# Slicer element tag -> scalar spec kind. Recovers the int/float split the
# widget table collapses to a single "number".
_SCALAR_KIND = {
    "integer": "int",
    "float": "float",
    "double": "float",
    "string": "string",
    "boolean": "bool",
}

_ENUM_TAGS = frozenset(
    (
        "integer-enumeration",
        "float-enumeration",
        "double-enumeration",
        "string-enumeration",
    )
)


def _json_number(value):
    """Canonicalize an integral float to ``int`` so emitted JSON matches the
    fixtures (``50.0`` -> ``50``); genuine fractionals pass through."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _finite_or_none(value):
    """Degrade a numeric that parsed to NaN/inf (see ``_parse_float``) to
    "no value". Without this the int path crashes ``int(nan)`` and the float
    path crashes Girder's JSON encoder (``allow_nan=False``) -- either way the
    whole task-spec endpoint 500s instead of dropping one messy default."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_integer(value):
    return _is_number(value) and float(value).is_integer()


def _is_identifier(value):
    return isinstance(value, str) and re.search(r"\S", value) is not None


def _structural_task_spec_issues(spec):
    """Return structural issues for the v1 task-spec shape this backend emits.

    This is the production-side equivalent of VolView's generated
    ``task-spec.schema.json``. Contract tests validate translated specs against
    that generated artifact as well, keeping this dependency-free runtime check
    pinned to the normative schema without copying the artifact into the plugin.
    """
    issues = []

    def issue(path, message):
        issues.append({"path": list(path), "message": message})

    if not isinstance(spec, dict):
        issue([], "spec must be an object")
        return issues
    unknown = set(spec) - {
        "specVersion",
        "id",
        "title",
        "description",
        "parameters",
        "outputs",
    }
    if unknown:
        issue([], "unknown spec fields: %s" % ", ".join(sorted(unknown)))
    if spec.get("specVersion") != _SPEC_VERSION:
        issue(["specVersion"], "unsupported spec version")
    taskId = spec.get("id")
    if not _is_identifier(taskId):
        issue(["id"], "id must not be empty")
    elif taskId in {".", ".."}:
        issue(["id"], "id must not be a dot segment")
    if not isinstance(spec.get("title"), str):
        issue(["title"], "title must be a string")
    if "description" in spec and not isinstance(spec["description"], str):
        issue(["description"], "description must be a string")

    parameters = spec.get("parameters")
    if not isinstance(parameters, list):
        issue(["parameters"], "parameters must be an array")
        parameters = []
    outputs = spec.get("outputs")
    if not isinstance(outputs, list):
        issue(["outputs"], "outputs must be an array")
        outputs = []

    common = {"kind", "id", "title", "help", "section", "order", "widget", "required"}
    kindFields = {
        "int": {"min", "max", "step", "default"},
        "float": {"min", "max", "step", "default"},
        "string": {"default"},
        "bool": {"default"},
        "enum": {"options", "default"},
        "sourceRef": {"accepts"},
        "bounds": {"default"},
    }
    for index, parameter in enumerate(parameters):
        path = ["parameters", index]
        if not isinstance(parameter, dict):
            issue(path, "parameter must be an object")
            continue
        kind = parameter.get("kind")
        if kind not in _PARAMETER_KINDS:
            issue(path + ["kind"], "unknown parameter kind")
            continue
        unknown = set(parameter) - common - kindFields[kind]
        if unknown:
            issue(path, "unknown parameter fields: %s" % ", ".join(sorted(unknown)))
        if not _is_identifier(parameter.get("id")):
            issue(path + ["id"], "id must not be empty")
        for field in ("title", "help", "section", "widget"):
            if field in parameter and not isinstance(parameter[field], str):
                issue(path + [field], "%s must be a string" % field)
        if "order" in parameter and not _is_number(parameter["order"]):
            issue(path + ["order"], "order must be a number")
        if "required" in parameter and not isinstance(parameter["required"], bool):
            issue(path + ["required"], "required must be a boolean")

        if kind in {"int", "float"}:
            numeric = _is_integer if kind == "int" else _is_number
            for field in ("min", "max", "step", "default"):
                if field in parameter and not numeric(parameter[field]):
                    issue(path + [field], "%s must be a %s" % (field, kind))
        elif kind == "string":
            if "default" in parameter and not isinstance(parameter["default"], str):
                issue(path + ["default"], "default must be a string")
        elif kind == "bool":
            if "default" in parameter and not isinstance(parameter["default"], bool):
                issue(path + ["default"], "default must be a boolean")
        elif kind == "enum":
            options = parameter.get("options")
            if not isinstance(options, list) or not options:
                issue(path + ["options"], "options must be a non-empty array")
            elif any(
                not (isinstance(option, str) or _is_number(option))
                for option in options
            ):
                issue(path + ["options"], "options must contain strings or numbers")
            if "default" in parameter and not (
                isinstance(parameter["default"], str)
                or _is_number(parameter["default"])
            ):
                issue(path + ["default"], "default must be a string or number")
        elif kind == "sourceRef":
            accepts = parameter.get("accepts")
            if (
                not isinstance(accepts, list)
                or not accepts
                or any(not isinstance(value, str) for value in accepts)
            ):
                issue(path + ["accepts"], "accepts must be a non-empty string array")
        elif kind == "bounds" and "default" in parameter:
            default = parameter["default"]
            if (
                not isinstance(default, list)
                or len(default) != 6
                or any(not _is_number(value) for value in default)
            ):
                issue(path + ["default"], "default must contain six numbers")

    outputFields = {"id", "title", "help", "type", "format"}
    for index, output in enumerate(outputs):
        path = ["outputs", index]
        if not isinstance(output, dict):
            issue(path, "output must be an object")
            continue
        unknown = set(output) - outputFields
        if unknown:
            issue(path, "unknown output fields: %s" % ", ".join(sorted(unknown)))
        if not _is_identifier(output.get("id")):
            issue(path + ["id"], "id must not be empty")
        for field in ("title", "help", "type", "format"):
            if field in output and not isinstance(output[field], str):
                issue(path + [field], "%s must be a string" % field)
    return issues


def task_spec_semantic_issues(spec):
    """Return the cross-field issues in VolView's v1 semantic validation pass."""
    if not isinstance(spec, dict):
        return []
    issues = []
    parameters = spec.get("parameters")
    if isinstance(parameters, list):
        for index, parameter in enumerate(parameters):
            if not isinstance(parameter, dict):
                continue
            path = ["parameters", index]
            kind = parameter.get("kind")
            if kind in {"int", "float"}:
                if (
                    _is_number(parameter.get("min"))
                    and _is_number(parameter.get("max"))
                    and parameter["min"] > parameter["max"]
                ):
                    issues.append(
                        {"path": path + ["min"], "message": "min must be <= max"}
                    )
                if (
                    _is_number(parameter.get("default"))
                    and _is_number(parameter.get("min"))
                    and parameter["default"] < parameter["min"]
                ):
                    issues.append(
                        {
                            "path": path + ["default"],
                            "message": "default must be >= min",
                        }
                    )
                if (
                    _is_number(parameter.get("default"))
                    and _is_number(parameter.get("max"))
                    and parameter["default"] > parameter["max"]
                ):
                    issues.append(
                        {
                            "path": path + ["default"],
                            "message": "default must be <= max",
                        }
                    )
                if _is_number(parameter.get("step")) and parameter["step"] <= 0:
                    issues.append(
                        {"path": path + ["step"], "message": "step must be > 0"}
                    )
            if (
                kind == "enum"
                and "default" in parameter
                and isinstance(parameter.get("options"), list)
                and parameter["default"] not in parameter["options"]
            ):
                issues.append(
                    {
                        "path": path + ["default"],
                        "message": "default must be one of the enum options",
                    }
                )

    for field in ("parameters", "outputs"):
        entries = spec.get(field)
        if not isinstance(entries, list):
            continue
        seen = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            identifier = entry["id"]
            if identifier in seen:
                label = "parameter" if field == "parameters" else "output"
                issues.append(
                    {
                        "path": [field, index, "id"],
                        "message": "duplicate %s id: %s" % (label, identifier),
                    }
                )
            seen.add(identifier)
    return issues


def validate_task_spec(spec):
    """Validate a translated spec structurally and semantically, or raise."""
    issues = _structural_task_spec_issues(spec) + task_spec_semantic_issues(spec)
    if issues:
        detail = "; ".join(
            "%s: %s" % (".".join(map(str, issue["path"])), issue["message"])
            for issue in issues
        )
        raise ValueError("Invalid VolView task spec: %s" % detail)
    return spec


def _image_accepts(image_type):
    """input ``<image>`` ``type`` -> ``sourceRef.accepts``.

    ``None`` (no type attr) or ``scalar`` -> ``["image"]``; ``label`` ->
    ``["labelmap"]``; anything else -> ``None``, signalling the caller to emit
    an unknown field kind (fail closed).
    """
    if image_type is None or image_type == "scalar":
        return ["image"]
    if image_type == "label":
        return ["labelmap"]
    return None


def _output_type(parsed):
    """Declared output ``type``: image outputs reuse the accepts vocabulary
    (image/labelmap); file outputs are ``file``; anything else passes through
    (outputs are an open vocabulary -- an unknown one has no state action)."""
    if parsed["tag"] == "file":
        return "file"
    image_type = parsed["imageType"]
    if image_type is None or image_type == "scalar":
        return "image"
    if image_type == "label":
        return "labelmap"
    return image_type


def _region_default_to_bounds(default_value):
    """Slicer ``<region>`` default -> VolView ``bounds``.

    A Slicer region default is ``cx,cy,cz,rx,ry,rz`` -- center + radius in RAS
    (Slicer's native frame). VolView ``bounds`` is an axis-aligned min/max box
    ``[xmin,xmax,ymin,ymax,zmin,zmax]`` in LPS, so RAS->LPS negates X and Y
    (swapping their min/max). Fail closed: a default that is not six parseable
    numbers yields *no* bounds default rather than a malformed one.

    No shipped radiology CLI carries a ``<region>`` default, so the golden
    fixtures do not pin this conversion; ``test_slicer_spec_translation.py``
    locks the convention.
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
    if not all(math.isfinite(v) for v in (cx, cy, cz, rx, ry, rz)):
        return None
    rx, ry, rz = abs(rx), abs(ry), abs(rz)
    x_lps = sorted((-(cx - rx), -(cx + rx)))
    y_lps = sorted((-(cy - ry), -(cy + ry)))
    z_lps = (cz - rz, cz + rz)
    return [
        _json_number(x_lps[0]),
        _json_number(x_lps[1]),
        _json_number(y_lps[0]),
        _json_number(y_lps[1]),
        _json_number(z_lps[0]),
        _json_number(z_lps[1]),
    ]


def _bounds_to_region(bounds):
    """VolView ``bounds`` (LPS min/max box) -> Slicer ``<region>`` center+radius (RAS).

    The exact inverse of ``_region_default_to_bounds``, applied at SUBMIT: the
    client mints a crop box as ``[xmin,xmax,ymin,ymax,zmin,zmax]`` in world LPS,
    but a Slicer ``<region>`` CLI param expects ``cx,cy,cz,rx,ry,rz`` -- center +
    radius in RAS (Slicer's native frame). So ``cx = -(xmin+xmax)/2`` and
    ``rx = (xmax-xmin)/2`` (LPS->RAS negates X and Y, so the center negates while
    the radius, a magnitude, stays non-negative); Y is negated the same way; Z
    passes through unmapped (``cz = (zmin+zmax)/2``, ``rz = (zmax-zmin)/2``).

    Fail closed: a value that is not six finite numbers yields ``None`` (the
    submit boundary turns that into a 400 naming the parameter) rather than a
    malformed region string reaching the CLI.
    """
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 6:
        return None
    try:
        xmin, xmax, ymin, ymax, zmin, zmax = (float(p) for p in bounds)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (xmin, xmax, ymin, ymax, zmin, zmax)):
        return None
    cx = -(xmin + xmax) / 2
    cy = -(ymin + ymax) / 2
    cz = (zmin + zmax) / 2
    rx = abs(xmax - xmin) / 2
    ry = abs(ymax - ymin) / 2
    rz = abs(zmax - zmin) / 2
    return [
        _json_number(cx),
        _json_number(cy),
        _json_number(cz),
        _json_number(rx),
        _json_number(ry),
        _json_number(rz),
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
    constraints = {
        k: v for k, v in parsed["constraints"].items() if _finite_or_none(v) is not None
    }
    default = _finite_or_none(parsed["default"])
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

    if tag == "image":
        accepts = _image_accepts(parsed["imageType"])
        if accepts is None:
            # unknown <image> type -> unknown field kind (fail closed).
            return {"kind": parsed["imageType"], **base}
        return {"kind": "sourceRef", **base, "accepts": accepts}
    if tag == "region":
        param = {"kind": "bounds", **base}
        bounds_default = _region_default_to_bounds(parsed["default"])
        if bounds_default is not None:
            param["default"] = bounds_default
        return param

    if tag in _ENUM_TAGS:
        # Numeric enum members canonicalize like the scalar path (1.0 -> 1);
        # string members pass through.
        param = {
            "kind": "enum",
            **base,
            "options": [
                _json_number(v)
                for v in (parsed["values"] or [])
                if _finite_or_none(v) is not None
            ],
        }
        default = _finite_or_none(parsed["default"])
        if default is not None:
            param["default"] = _json_number(default)
        return param
    if tag in _SCALAR_KIND:
        return _translate_scalar(_SCALAR_KIND[tag], parsed, base)

    # anything else -> unknown field kind (fail closed)
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
            # not spec fields.
            if parsed["tag"] in ("image", "file"):
                outputs.append(_translate_output(parsed))
            continue
        if parsed["id"] in _RESERVED_INPUT_PARAMS:
            # Skip before ``order`` advances so the remaining params keep their
            # fixture-pinned order numbers.
            continue
        parameters.append(_translate_param(parsed, order))
        order += 1

    spec = {"specVersion": _SPEC_VERSION, "id": task_id, "title": doc["title"]}
    if doc["description"]:
        spec["description"] = doc["description"]
    spec["parameters"] = parameters
    spec["outputs"] = outputs
    return spec
