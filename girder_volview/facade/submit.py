"""Processing facade -- the slicer_cli_web submit bridge (Seam 2 request half).

Split out of the former monolith ``processing.py`` (Chunk 32, pure code motion).
This module owns everything between "the client picked a task and filled a form"
and "hand slicer_cli_web the form-encoded params":

- the slicer_cli_web catalog bridge (``_listCliItems`` / ``_findCliItem`` / ...);
- radiology task scoping by ``<category>`` (D11), now reading the single
  ``slicer_spec.parse_cli`` walk instead of a private duplicate XML parse;
- the COSMETIC output-naming cluster (see its section note); and
- the values → slicer_cli_web params translation (D10 v1 = b3).

The actual job creation (``_genDockerJob``) and the REST handlers live in
``routes.py``; result correlation/collection live in ``outputs.py`` /
``results.py``.
"""

from girder.exceptions import RestException

from .inputs import resolveInputUrisToFileIds, resolveSourceRefToFolder
from .slicer_spec import parse_cli, _RESERVED_INPUT_PARAMS


# ---------------------------------------------------------------------------
# slicer_cli_web bridge
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task scoping (D11 part 2, item 3.5) — filter the CLI catalog by <category>
#
# ``listTasks`` would otherwise return EVERY registered slicer_cli_web CLI, so a
# radiology VolView's dropdown also lists the HistomicsTK *pathology* CLIs
# (NucleiDetection, ColorDeconvolution, …) and volview_dicomrt. We keep only
# CLIs whose Slicer XML ``<category>`` is in an allowed set (default radiology).
# The radiology CLIs ship ``<category>Radiology</category>`` and the pathology
# CLIs declare pathology categories (``HistomicsTK``), so the filter is
# self-describing and needs no per-image allow-list — new radiology CLIs are
# included automatically (decisions.md D11).
#
# The *server* is the boundary: ``getTaskSpec``/``runTask`` 404 a filtered-out
# taskId exactly like an unknown id, so scoping can't be bypassed by guessing
# an id. Fail-closed: a CLI with no/unknown ``<category>`` is excluded. The
# ``<category>`` is read from the single ``slicer_spec.parse_cli`` walk.
# ---------------------------------------------------------------------------

# Default radiology category set (matched case-insensitively). The three shipped
# radiology CLIs all declare ``<category>Radiology</category>``; Segmentation /
# Filtering cover radiology operations a future CLI might categorize under and
# are disjoint from the pathology CLIs' ``HistomicsTK`` category.
_DEFAULT_ALLOWED_CATEGORIES = ("Radiology", "Segmentation", "Filtering")
# Comma-separated env override for other deployments. Empty/unset falls back to
# the default set, never to "unfiltered" (scoping is a locked requirement, D11).
_ALLOWED_CATEGORIES_ENV = "VOLVIEW_PROCESSING_ALLOWED_CATEGORIES"


def _allowedCategories():
    """Allowed CLI ``<category>`` names, lowercased, from env or the default set."""
    import os
    raw = os.environ.get(_ALLOWED_CATEGORIES_ENV) or ""
    override = {c.strip().lower() for c in raw.split(",") if c.strip()}
    return override or {c.lower() for c in _DEFAULT_ALLOWED_CATEGORIES}


def _taskInScope(cliItem, allowed=None):
    """Whether a CLI's ``<category>`` is in the allowed scope (fail-closed).

    A CLI with no/unknown ``<category>`` is excluded so scoping can't be
    bypassed. ``allowed`` (lowercased set) is passed in by ``_scopedCliItems``
    to parse the env once per request; the single-task callers omit it. The
    category is read from the single ``slicer_spec.parse_cli`` walk (a parse
    failure yields ``category=None`` → out of scope).
    """
    if allowed is None:
        allowed = _allowedCategories()
    try:
        category = parse_cli(cliItem.xml)["category"]
    except Exception:
        return False
    return category is not None and category.lower() in allowed


def _scopedCliItems(user):
    """CLIItems whose declared ``<category>`` is in the allowed scope (D11).

    The exact set ``listTasks`` advertises; the pathology CLIs never reach the
    client.
    """
    allowed = _allowedCategories()
    return [c for c in _listCliItems(user) if _taskInScope(c, allowed)]


def _findScopedCliItem(taskId, user):
    """Resolve a taskId to an in-scope CLIItem, or None for the caller to 404.

    Out-of-scope tasks resolve to None exactly like unknown ids, so a filtered
    pathology CLI can't be reached by guessing its id.
    """
    cliItem = _findCliItem(taskId, user)
    if not cliItem or not _taskInScope(cliItem):
        return None
    return cliItem


# ---------------------------------------------------------------------------
# Output naming — COSMETIC ONLY (ARCHITECTURE-REVIEW §5.2)
#
# Correlation is REFERENCE-BOUND, not name-matched (contract Seam 3): a job's
# outputs bind to it by the slicer_cli_web output ``identifier`` + per-run token
# recorded on the job (``outputs.py`` / ``results.py``), NEVER by filename. So
# every function in this cluster (``_splitExt`` / ``_candidateOutputName`` /
# ``_outputExtension`` / ``_firstInputBaseName`` / ``_autofillOutputs``) exists
# only to hand a human a readable default filename. Nothing load-bearing reads
# the name it produces; changing the naming scheme cannot cross or lose a result.
# ---------------------------------------------------------------------------

# Compound extensions we want to preserve as a single suffix.
_COMPOUND_EXTENSIONS = (
    ".nii.gz", ".tar.gz", ".mgh.gz", ".hdr.gz", ".mnc.gz",
    ".iwi.cbor.zst", ".iwi.cbor",
)


def _splitExt(name):
    """Like os.path.splitext but recognizes radiology compound extensions.

    Cosmetic only (see the section note): feeds the human-readable default
    output filename; nothing load-bearing parses the result.
    """
    lower = name.lower()
    for ext in _COMPOUND_EXTENSIONS:
        if lower.endswith(ext):
            return name[: -len(ext)], name[-len(ext):]
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

    Cosmetic only (see the section note): only shapes the default filename.
    """
    raw = out.get("fileExtensions") or ""
    for ext in raw.split(","):
        ext = ext.strip()
        if ext:
            return ext if ext.startswith(".") else "." + ext
    return _defaultExtensionForOutput(out)


def _candidateOutputName(inputBase, cliName, paramName, ext):
    """Build a deterministic candidate name; uniquifying is a separate step.

    Cosmetic only (see the section note): the returned name is a human-facing
    default; correlation binds by reference, never by this string.
    """
    base = (inputBase or "output").strip(". ")
    cli = (cliName or "task").strip(". ")
    return f"{base}.{cli}.{paramName}{ext}"


def _firstInputBaseName(values):
    """Base name (no extension) of the first client-minted input, for naming.

    Pure string parse of the input value's first uri (the minted
    ``…/proxiable/<name>`` — its last path segment is the original filename), so
    an auto-generated output reads ``<inputname>.<cli>.<param><ext>``. No file
    load, no ACL — this only seeds a (cosmetic) name; the real
    resolution/validation happens in ``_translateValuesToSlicerParams``. Falls
    back to ``"output"`` when there is no usable input uri.
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
        base, _ = _splitExt(first.rsplit("/", 1)[-1])
        base = base.strip(". ")
        if base:
            return base
    return "output"


def _autofillOutputs(values, cli_xml, cli_name):
    """Auto-generate a deterministic name for output params the client didn't fill.

    COSMETIC ONLY (see the section note). Mutates and returns `values`. Output
    param values become `ProcessingOutputRequest`-style dicts:
    `{"name": "<candidate>", ...}`.

    The name is deterministic (`<input>.<cli>.<param><ext>`) and NOT uniquified:
    the old check-then-use `while findOne(name)` folder scan was itself racy (two
    concurrent submits both saw a name free and both took it) and is now needless —
    outputs bind to the job by reference (`_recordJobOutput`), never by filename, so
    two jobs writing the same name into one folder no longer cross results (D5). The
    output descriptors come from the single ``slicer_spec.parse_cli`` walk.
    """
    outputs = parse_cli(cli_xml or "")["outputs"]
    if not outputs:
        return values

    inputBase = _firstInputBaseName(values)

    for out in outputs:
        existing = values.get(out["name"])
        if isinstance(existing, dict) and existing.get("name"):
            continue
        ext = _outputExtension(out)
        candidate = _candidateOutputName(inputBase, cli_name, out["name"], ext)
        new_value = {"name": candidate}
        if isinstance(existing, dict):
            new_value.update({k: v for k, v in existing.items() if k != "name"})
        values[out["name"]] = new_value
    return values


# ---------------------------------------------------------------------------
# Values → slicer_cli_web params (D10 v1 = b3)
#
# A bound input arrives as the client-minted ``{type, format?, uris}`` value; the
# facade resolves the URIs back to Girder file ids (own-scheme validation +
# per-user ACL re-check) and forwards them to the CLI as a ``<string>`` param —
# comma-joined for a multi-file volume (a DICOM series = N ids). ``slicer_cli_web``
# injects ``girderApiUrl``/``girderToken`` (``prepare_task.py``/``cli_utils.py`` —
# zero upstream change) and the CLI fetches + assembles: the CLI sees ids + a
# token, never a URL, and the facade never touches pixels.
# ---------------------------------------------------------------------------

def _rejectReservedSubmitParams(values):
    """Fail closed on a submission that smuggles reserved/undeclared params.

    A separate submit-time defense from the spec-side drop
    (``slicer_spec._RESERVED_INPUT_PARAMS``): the translator never *emits* these
    to the client form, and this rejects a hand-crafted submit that tries to feed
    them back in (Chunk 21, D9 addendum). Screens the RAW client-submitted keys —
    before the facade derives any ``{param}_folder`` output-destination param — so
    it never trips over the facade's own output plumbing. Rejects, never strips.

    - ``girderApiUrl`` / ``girderToken``: ``slicer_cli_web``'s injected b3
      credentials; a client value would try to redirect the CLI's girder client
      or swap out its token.
    - ``*_folder``: the facade synthesizes ``{param}_folder`` server-side
      (``_translateValuesToSlicerParams``); the client never declares one, so any
      ``*_folder`` in the raw submission is undeclared and rejected.
    """
    offending = sorted(
        key
        for key in (values or {})
        if key in _RESERVED_INPUT_PARAMS or key.endswith("_folder")
    )
    if offending:
        raise RestException(
            "Reserved parameter(s) may not be submitted: %s" % ", ".join(offending),
            code=400,
        )


def _translateValuesToSlicerParams(values, user, folder):
    """Translate a VolView values payload to slicer_cli_web's form-encoded params.

    - Client-minted input values ``{type, format?, uris}`` → resolved Girder file
      ids, forwarded as a ``<string>`` param (comma-joined for N files; b3).
    - ``ProcessingOutputRequest`` outputs → name + name_folder (output goes back
      to the launching folder by default).
    - Scalars / plain strings / lists → their string form.
    """
    params = {}
    for paramName, value in (values or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            params[paramName] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            params[paramName] = str(value)
        elif isinstance(value, dict) and "uris" in value:
            # A bound input: resolve the facade's own URIs back to file ids
            # (strict validation + ACL re-check) and forward the ids (b3).
            fileIds = resolveInputUrisToFileIds(value.get("uris"), user)
            params[paramName] = ",".join(fileIds)
        elif isinstance(value, dict) and "name" in value:
            # ProcessingOutputRequest
            params[paramName] = value["name"]
            outFolderRef = value.get("folderRef")
            if outFolderRef:
                outFolder = resolveSourceRefToFolder(outFolderRef, user)
            else:
                outFolder = folder
            params[f"{paramName}_folder"] = str(outFolder["_id"])
        elif isinstance(value, str):
            params[paramName] = value
        elif isinstance(value, list):
            params[paramName] = ",".join(str(v) for v in value)
        else:
            params[paramName] = str(value)
    return params
