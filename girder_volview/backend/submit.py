"""The slicer_cli_web submit bridge: catalog lookup, task scoping by
``<category>``, output naming, and the values → form-encoded params translation.

Job creation and the REST handlers live in ``routes.py``; result
correlation/collection live in ``outputs.py`` / ``results.py``.
"""

import functools
import os

from girder.exceptions import RestException

from ..handles import parseFileHandle
from .inputs import resolveInputUrisToFiles
from .slicer_spec import (
    parse_cli,
    _bounds_to_region,
    _json_number,
    _RESERVED_INPUT_PARAMS,
)

# slicer_cli_web's output-destination convention: for each output param the
# submission carries a derived ``{param}_folder`` param naming the destination
# folder. One symbol ties the server-side emit
# (``_translateValuesToSlicerParams``) to the submit-time reject of a
# client-supplied colliding key (``_rejectSynthesizedFolderParams``).
_OUTPUT_FOLDER_SUFFIX = "_folder"


def _slicerCliAvailable():
    try:
        import slicer_cli_web  # noqa: F401

        return True
    except ImportError:
        return False


def _listCliItems(user):
    """Return CLIItem instances visible to the user."""
    from slicer_cli_web.models import CLIItem

    return list(CLIItem.findAllItems(user))


def _findCliItem(taskId, user):
    """Resolve a taskId to a CLIItem. taskId is the underlying Item._id."""
    from slicer_cli_web.models import CLIItem

    item = CLIItem.find(taskId, user)
    return item


def _cliItemToSummary(cliItem):
    return {
        "id": str(cliItem._id),
        "title": cliItem.name,
        "description": cliItem.item.get("description", ""),
        "dockerImage": cliItem.image,
    }


# Default category set (matched case-insensitively). Segmentation / Filtering
# cover radiology operations a future CLI might categorize under and are
# disjoint from the pathology CLIs' ``HistomicsTK`` category.
_DEFAULT_ALLOWED_CATEGORIES = ("Radiology", "Segmentation", "Filtering")
# Comma-separated env override for other deployments. Empty/unset falls back to
# the default set, never to "unfiltered".
_ALLOWED_CATEGORIES_ENV = "VOLVIEW_PROCESSING_ALLOWED_CATEGORIES"


@functools.lru_cache(maxsize=8)
def _parseAllowedCategories(raw):
    override = frozenset(c.strip().lower() for c in raw.split(",") if c.strip())
    return override or frozenset(c.lower() for c in _DEFAULT_ALLOWED_CATEGORIES)


def _allowedCategories():
    """Allowed CLI ``<category>`` names, lowercased, from env or the default set.

    The env read stays per-call (tests monkeypatch it); only the split/lowercase
    of a given raw string is memoized.
    """
    return _parseAllowedCategories(os.environ.get(_ALLOWED_CATEGORIES_ENV) or "")


def _categoryInScope(category, allowed=None):
    """Whether a parsed ``<category>`` is in the allowed scope (fail-closed).

    A CLI with no/unknown ``<category>`` is excluded so scoping can't be
    bypassed. Takes a parsed category so callers holding a ``parse_cli`` result
    need not re-parse.
    """
    if allowed is None:
        allowed = _allowedCategories()
    return category is not None and category.lower() in allowed


@functools.lru_cache(maxsize=256)
def _cliCategory(xml_text):
    """The CLI's parsed ``<category>``, memoized by document.

    ``_scopedCliItems`` re-screens the whole catalog on every listTasks request,
    but a CLI's XML changes only when its docker image is (re)registered, so the
    category-only parse is cached on the xml string. Only this scalar is cached
    — the full ``parse_cli`` structures are mutable and stay per-call.
    """
    return parse_cli(xml_text)["category"]


def _taskInScope(cliItem, allowed=None):
    """Whether a CLI's ``<category>`` is in the allowed scope (fail-closed).

    A CLI with no/unknown ``<category>`` is excluded so scoping can't be
    bypassed; a parse failure is likewise out of scope. ``allowed`` (lowercased
    set) is passed in by ``_scopedCliItems`` so the env is parsed once per
    request; omitting it re-reads the env per call.
    """
    try:
        category = _cliCategory(cliItem.xml)
    except Exception:
        return False
    return _categoryInScope(category, allowed)


def _scopedCliItems(user):
    """CLIItems whose declared ``<category>`` is in the allowed scope.

    The exact set ``listTasks`` advertises; the pathology CLIs never reach the
    client.
    """
    allowed = _allowedCategories()
    return [c for c in _listCliItems(user) if _taskInScope(c, allowed)]


def _findScopedCliItem(taskId, user):
    """Resolve a taskId to an in-scope ``(CLIItem, parsedCli)``, or None to 404.

    Parses the CLI XML once and returns that parsed structure alongside the item
    so the caller reuses it rather than re-parsing. Out-of-scope tasks resolve to
    ``None`` exactly like unknown ids, so a filtered pathology CLI can't be
    reached by guessing its id.
    """
    cliItem = _findCliItem(taskId, user)
    if not cliItem:
        return None
    try:
        parsed = parse_cli(cliItem.xml)
    except Exception:
        return None
    if not _categoryInScope(parsed["category"]):
        return None
    return cliItem, parsed


# The composed name becomes the output filename the worker writes on the
# container host, so it must be a server-generated basename: every component is
# collapsed through ``_safeNameToken`` and a client-supplied name is discarded.
# Correlation itself binds by reference (``outputs.py`` / ``results.py``), never
# by this string.

# Compound extensions we want to preserve as a single suffix.
_COMPOUND_EXTENSIONS = (
    ".nii.gz",
    ".tar.gz",
    ".mgh.gz",
    ".hdr.gz",
    ".mnc.gz",
    ".iwi.cbor.zst",
    ".iwi.cbor",
)


def _splitExt(name):
    """Like os.path.splitext but recognizes radiology compound extensions.

    Cosmetic only: feeds the default output filename; nothing parses the result.
    """
    lower = name.lower()
    for ext in _COMPOUND_EXTENSIONS:
        if lower.endswith(ext):
            return name[: -len(ext)], name[-len(ext) :]
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]


def _defaultExtensionForOutput(out):
    """Pick a sensible extension when the CLI didn't declare one (cosmetic only)."""
    if out["tag"] == "image":
        return ".nii.gz"
    return ".dat"


def _outputExtension(out):
    """Return the first declared fileExtension, or a tag-based default.

    Cosmetic only: shapes the default filename and nothing else.
    """
    raw = out.get("fileExtensions") or ""
    for ext in raw.split(","):
        ext = ext.strip()
        if ext:
            return ext if ext.startswith(".") else "." + ext
    return _defaultExtensionForOutput(out)


def _safeNameToken(token, fallback):
    """Collapse a name component to a single separator-free path token.

    The composed output name becomes a worker-host filename, so every component
    MUST be a plain basename: an input-handle name can decode to
    ``safe/../../etc/passwd`` (``%2F`` survives the handle parser's pre-decode
    slash check), and a bare ``strip``-style cleanup would pass the traversal
    through. Takes the last ``/``- or ``\\``-separated segment, strips edge
    dots/spaces, and falls back when nothing safe is left.
    """
    token = str(token or "").replace("\\", "/").rsplit("/", 1)[-1]
    token = token.strip(". ")
    return token or fallback


def _candidateOutputName(inputBase, cliName, paramName, ext):
    """Build a deterministic candidate name.

    Correlation binds by reference, never by this string, but the name IS the
    worker-host output filename, so every component is collapsed to a safe
    single token through the ``_safeNameToken`` chokepoint.
    """
    base = _safeNameToken(inputBase, "output")
    cli = _safeNameToken(cliName, "task")
    param = _safeNameToken(paramName, "out")
    return f"{base}.{cli}.{param}{ext}"


def _firstInputBaseName(values):
    """Base name (no extension) of the first client-minted input, for naming.

    Pure string parse of the input value's first uri (``parseFileHandle``
    recovers the unescaped filename from the minted ``…/proxiable/<name>``; a
    foreign uri falls back to its last path segment). No file load, no ACL —
    this only seeds a name; the real resolution and validation happen in
    ``_translateValuesToSlicerParams``. Falls back to ``"output"`` when there is
    no usable input uri.
    """
    for value in (values or {}).values():
        if not isinstance(value, dict):
            continue
        uris = value.get("uris")
        if not isinstance(uris, list) or not uris:
            continue
        first = uris[0]
        if not isinstance(first, str) or not first:
            continue
        parsed = parseFileHandle(first)
        name = parsed[1] if parsed else first.rsplit("/", 1)[-1]
        # A parsed handle name is percent-DECODED and may contain slashes the
        # handle grammar never saw; basename it before use (defense in depth —
        # _candidateOutputName re-sanitizes every component regardless).
        name = _safeNameToken(name, "")
        base, _ = _splitExt(name)
        base = base.strip(". ")
        if base:
            return base
    return "output"


# The ProcessingOutputRequest wire shape: the server owns ``name``
# (``_autofillOutputs`` overwrites it); ``format`` is the one client-chosen key.
_OUTPUT_REQUEST_KEYS = frozenset({"name", "format"})


def _autofillOutputs(values, outputs, cli_name):
    """Generate the SERVER-OWNED name for every declared output param.

    The output filename is server-owned, never client-selected: a client-supplied
    ``name`` such as ``../../../../etc/passwd`` would be a worker-host
    path-traversal vector, so this ALWAYS overwrites ``name`` with the
    deterministic server-side value and discards any client-supplied one. Only
    the other ``_OUTPUT_REQUEST_KEYS`` on an existing output dict value are
    merged through — the merge is what rides into the job's recorded submission,
    so it never propagates a key the wire shape doesn't own (validation already
    400s unknown keys with a message naming them). Mutates and returns `values`;
    output param values become `ProcessingOutputRequest`-style dicts:
    `{"name": "<candidate>", ...}`.

    The name is deterministic (`<input>.<cli>.<param><ext>`) and NOT uniquified:
    outputs bind to the job by reference (`_recordJobOutput`), never by filename,
    so two jobs writing the same name into one folder do not cross results.
    ``outputs`` is the parsed CLI descriptor list ``runTask`` threads in.
    """
    if not outputs:
        return values

    inputBase = _firstInputBaseName(values)

    for out in outputs:
        existing = values.get(out["name"])
        ext = _outputExtension(out)
        candidate = _candidateOutputName(inputBase, cli_name, out["name"], ext)
        new_value = {"name": candidate}
        if isinstance(existing, dict):
            # Merge only the client-owned wire keys, never the server-owned name.
            new_value.update(
                {
                    k: v
                    for k, v in existing.items()
                    if k in _OUTPUT_REQUEST_KEYS and k != "name"
                }
            )
        values[out["name"]] = new_value
    return values


# Inputs cross to the CLI as Girder file ids, never URLs: ``slicer_cli_web``
# injects ``girderApiUrl``/``girderToken`` and the CLI fetches + assembles the
# bytes itself, so the backend never touches pixels.


def _rejectReservedSubmitParams(values):
    """Fail closed on a submission that smuggles reserved credential params.

    A separate submit-time defense from the spec-side drop
    (``slicer_spec._RESERVED_INPUT_PARAMS``): the translator never *emits* these
    to the client form, and this rejects a hand-crafted submit that tries to feed
    them back in. ``girderApiUrl`` / ``girderToken`` are ``slicer_cli_web``'s
    injected credentials; a client value would try to redirect the CLI's girder
    client or swap out its token. Screens the RAW client-submitted keys before
    any task lookup. Rejects, never strips.
    """
    offending = sorted(key for key in (values or {}) if key in _RESERVED_INPUT_PARAMS)
    if offending:
        raise RestException(
            "Reserved parameter(s) may not be submitted: %s" % ", ".join(offending),
            code=400,
        )


def _rejectSynthesizedFolderParams(values, declared):
    """Fail closed on a raw key that collides with a synthesized folder param.

    The backend derives ``{output}_folder`` output-destination params server-side
    (``_translateValuesToSlicerParams``); a client-submitted collision would try
    to redirect where an output is written. Only names the translator actually
    synthesizes are reserved — a CLI is free to declare its own ``*_folder``
    param, and any *undeclared* ``*_folder`` key already dies in
    ``_rejectUndeclaredSubmitParams``. Rejects, never strips.
    """
    synthesized = {
        name + _OUTPUT_FOLDER_SUFFIX
        for name, decl in (declared or {}).items()
        if decl.get("channel") == "output" and decl.get("tag") in ("image", "file")
    }
    offending = sorted(key for key in (values or {}) if key in synthesized)
    if offending:
        raise RestException(
            "Reserved parameter(s) may not be submitted: %s" % ", ".join(offending),
            code=400,
        )


def _rejectUndeclaredSubmitParams(values, declared):
    """Fail closed (400) on a submission key the CLI does not declare.

    The submit boundary accepts only keys the task's Slicer CLI actually declares
    as parameters. A hand-crafted payload smuggling an undeclared key (a typo, a
    probe, or a client-authored output structure under an unknown name) is rejected
    here with a clear 400 rather than silently ignored or 500-ing downstream. The
    reserved-param screens (``_rejectReservedSubmitParams``,
    ``_rejectSynthesizedFolderParams``) run first, so
    ``girderApiUrl``/``girderToken`` and synthesized-folder collisions take that
    typed rejection even though the CLI declares the credential params in its XML.

    ``declared`` is the ``slicer_spec.declared_params`` mapping ``runTask`` parsed
    once; its key set is exactly the accepted names.
    """
    offending = sorted(key for key in (values or {}) if key not in declared)
    if offending:
        raise RestException(
            "Undeclared parameter(s) may not be submitted: %s" % ", ".join(offending),
            code=400,
        )


# The key guards above establish WHICH names may be submitted; these validate
# the VALUES against ``slicer_spec.declared_params``. They branch by
# DECLARATION, unlike ``_translateValuesToSlicerParams`` which branches by value
# shape: a declared OUTPUT carrying ``uris`` shape-matches the translator's input
# branch, loses its output-folder param, and dies downstream.
#
# Deliberately NOT validated here: ``region`` values (validated where they are
# converted, in ``_regionParamToSlicerValue``); ``item``/``directory``/``multi``
# values (no scalar wire contract — the CLI validates); constraint ranges on
# vector ELEMENTS (no shipped radiology CLI declares them); and uri strings
# inside input objects (``resolveInputUrisToFiles`` owns scheme validation and
# the ACL re-check).


def _isNumber(value):
    # bool is an int subclass in Python; a submitted true/false is never a number.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _isIntegral(value):
    # JSON carries no int/float distinction, so an integer param accepts 5.0.
    return _isNumber(value) and float(value).is_integer()


def _rangeProblem(value, constraints):
    minimum = constraints.get("min")
    maximum = constraints.get("max")
    if minimum is not None and value < minimum:
        return "is below the declared minimum %s" % _formatNumber(minimum)
    if maximum is not None and value > maximum:
        return "is above the declared maximum %s" % _formatNumber(maximum)
    return None


def _formatNumber(value):
    # Render an integral float as its int form (50.0 -> "50") in 400 messages
    # and CLI param strings; bools, strings, and genuine fractionals pass
    # through untouched.
    return str(_json_number(value))


def _vectorElements(value):
    """The two wire forms the translator forwards: a JSON list, or a pre-joined
    comma-separated string. Anything else is not a vector."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return value.split(",")
    return None


def _numberElementOk(element, integral):
    if isinstance(element, str):
        try:
            element = float(element.strip())
        except ValueError:
            return False
    if not _isNumber(element) or element != element:  # non-number or NaN
        return False
    return not integral or float(element).is_integer()


def _scalarProblem(decl, value):
    """Type/range/enum mismatch for a declared scalar, or None when it passes."""
    widget = decl["widget"]
    if widget == "boolean":
        return None if isinstance(value, bool) else "expected a boolean"
    if widget == "string":
        return None if isinstance(value, str) else "expected a string"
    if widget == "number":
        if not _isNumber(value):
            return "expected a number"
        if decl["tag"] == "integer" and not _isIntegral(value):
            return "expected an integer"
        return _rangeProblem(value, decl["constraints"])
    if widget in ("number-enumeration", "string-enumeration"):
        options = decl["options"] or []
        typed_ok = (
            isinstance(value, str)
            if widget == "string-enumeration"
            else _isNumber(value)
        )
        if not typed_ok or value not in options:
            return "expected one of: %s" % ", ".join(
                _formatNumber(o) for o in options
            )
        return None
    if widget in ("number-vector", "string-vector"):
        elements = _vectorElements(value)
        if elements is None:
            return "expected a list (or comma-separated string) of elements"
        if widget == "string-vector":
            bad = not all(isinstance(e, str) for e in elements)
            kind = "a string"
        else:
            integral = decl["tag"] == "integer-vector"
            bad = not all(_numberElementOk(e, integral) for e in elements)
            kind = "an integer" if integral else "a number"
        if bad:
            return "expected every vector element to be %s" % kind
        return None
    return None


def _submitValueProblem(decl, value):
    """Why a submitted value mismatches its CLI declaration, or None."""
    # Declared image/file OUTPUTS are server-composed: the name is overwritten
    # and unknown keys are dropped by ``_autofillOutputs``, and a folderRef is
    # rejected in translation. Reject the same shapes here so the submitter gets
    # a boundary 400 naming the problem instead of a silently pruned value.
    if decl["channel"] == "output" and decl["tag"] in ("image", "file"):
        if isinstance(value, dict):
            if "uris" in value:
                return "output values may not carry uris (outputs are server-composed)"
            unknown = sorted(set(value) - _OUTPUT_REQUEST_KEYS)
            if unknown:
                return "unexpected output key(s): %s" % ", ".join(unknown)
        return None
    # Declared image/file INPUTS are the client-minted {type, format?, uris}
    # objects; anything else would be stringified and forwarded as garbage.
    if decl["widget"] in ("image", "file"):
        uris = value.get("uris") if isinstance(value, dict) else None
        if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
            return "expected an input object with a uris list"
        return None
    return _scalarProblem(decl, value)


def _validateDeclaredSubmitValues(values, declared):
    """Fail closed (400) on a declared param whose submitted VALUE mismatches the
    CLI's declaration (type / <constraints> range / enumeration membership).

    Runs after ``_rejectUndeclaredSubmitParams`` (every surviving key is declared)
    and before ``_autofillOutputs``/translation, so a mismatch is a boundary 400
    naming the parameter instead of a stringified value failing inside the job.
    ``None`` values are skipped exactly as the translator skips them. ``declared``
    is the ``slicer_spec.declared_params`` mapping ``runTask`` parsed once.
    """
    problems = sorted(
        "%s (%s)" % (name, problem)
        for name, value in (values or {}).items()
        if value is not None and name in declared
        for problem in (_submitValueProblem(declared[name], value),)
        if problem
    )
    if problems:
        raise RestException(
            "Invalid value for declared parameter(s): %s" % "; ".join(problems),
            code=400,
        )


def _rejectMissingRequiredParams(values, declared):
    """Fail closed (400) when a required (indexed) param is absent or ``None``.

    Declared outputs are exempt: ``_autofillOutputs`` composes any output the
    user leaves unfilled. Without this guard a missing positional input passes
    both key screens (they inspect only present, non-``None`` keys) and the job
    dies inside the container with a cryptic argparse error instead of a
    boundary 400 naming the parameter.
    """
    missing = sorted(
        name
        for name, decl in (declared or {}).items()
        if decl.get("required")
        and decl.get("channel") != "output"
        and (values or {}).get(name) is None
    )
    if missing:
        raise RestException(
            "Missing required parameter(s): %s" % ", ".join(missing),
            code=400,
        )


def _regionParamToSlicerValue(paramName, value):
    """Convert a ``<region>`` param's client bounds box to Slicer's wire grammar.

    A ``<region>`` CLI param expects ``cx,cy,cz,rx,ry,rz`` (center + radius, RAS),
    but the client mints the value as an LPS min/max box; ``_bounds_to_region``
    inverts the frame. Fail closed: a value that is not six finite numbers is a
    boundary 400 naming the parameter rather than a malformed region string
    silently reaching the CLI (which would process a wrong spatial region).
    """
    region = _bounds_to_region(value)
    if region is None:
        raise RestException(
            "Invalid value for region parameter '%s': expected a bounds box of "
            "six finite numbers" % paramName,
            code=400,
        )
    return ",".join(str(v) for v in region)


def _translateValuesToSlicerParams(values, user, outputFolder, declared=None):
    """Translate a VolView values payload to slicer_cli_web's form-encoded params.

    - Client-minted input values ``{type, format?, uris}`` → resolved Girder file
      ids, forwarded as a ``<string>`` param (comma-joined for N files).
    - ``ProcessingOutputRequest`` outputs → name + name_folder, FORCED into the
      job's server-created private output folder (``outputFolder``). Output
      location is server-owned: a client-supplied ``folderRef`` is rejected, so a
      submission can never redirect a job's outputs out of its own folder.
    - ``<region>`` params (identified by their declaration, not value shape) →
      the client's LPS bounds box inverted to Slicer's RAS center+radius grammar
      (``_regionParamToSlicerValue``); a malformed box is a boundary 400.
    - Scalars / plain strings / lists → their string form.

    ``declared`` is the ``slicer_spec.declared_params`` mapping ``runTask`` parsed
    once; it drives the region-param branch by DECLARED tag (a ``region`` value is
    an ordinary list on the wire, so it cannot be recognized by shape). Callers
    that pass no ``declared`` mapping carry no region params.

    Returns the translated params and the authorized input file documents. The
    caller reuses those documents for transient-item detection, so each URI's
    ACL check is performed exactly once per submission.
    """
    declared = declared or {}
    params = {}
    resolvedInputFiles = {}
    for paramName, value in (values or {}).items():
        if value is None:
            continue
        if declared.get(paramName, {}).get("tag") == "region":
            # A <region> param: invert the client's LPS box to Slicer's RAS
            # center+radius grammar (fail closed on a malformed box). Handled by
            # declaration before the generic list branch, which would otherwise
            # comma-join the raw min/max box in the wrong convention.
            params[paramName] = _regionParamToSlicerValue(paramName, value)
        elif isinstance(value, bool):
            params[paramName] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            # Canonical int form for integral floats: validation accepts 5.0
            # for an <integer> param (JSON has no int/float split), but the
            # CLI's argparse int()/enum parsing would reject the "5.0" string.
            params[paramName] = _formatNumber(value)
        elif isinstance(value, dict) and "uris" in value:
            # A bound input: resolve the backend's own URIs back to file ids
            # (strict validation + ACL re-check) and forward the ids.
            fileDocs = resolveInputUrisToFiles(value.get("uris"), user)
            resolvedInputFiles[paramName] = fileDocs
            params[paramName] = ",".join(str(fileDoc["_id"]) for fileDoc in fileDocs)
        elif isinstance(value, dict) and "name" in value:
            # ProcessingOutputRequest. Output location is SERVER-OWNED: every
            # declared output is forced into the job's private output folder. A
            # client-supplied folderRef is rejected (not honored, not stripped) so
            # a job's outputs can never be redirected out of its own folder --
            # which is also the only output-correlation key.
            if "folderRef" in value:
                raise RestException(
                    "Output folderRef is server-owned and may not be submitted",
                    code=400,
                )
            params[paramName] = value["name"]
            params[paramName + _OUTPUT_FOLDER_SUFFIX] = str(outputFolder["_id"])
        elif isinstance(value, str):
            params[paramName] = value
        elif isinstance(value, list):
            # Same canonical int form per element: validation accepts 5.0 for an
            # <integer-vector> element, but the CLI would reject "5.0".
            params[paramName] = ",".join(_formatNumber(v) for v in value)
        else:
            params[paramName] = str(value)
    return params, resolvedInputFiles
