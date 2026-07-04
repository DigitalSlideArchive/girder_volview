"""Processing facade — provider config + slicer-cli proxy for VolView.

Translates VolView-native processing requests into `slicer_cli_web` calls and
projects Girder jobs back into the VolView provider contract.

Input resolution (Seam 1, client-processing-contract.md):
- The client mints nothing backend-specific. Per bound input it round-trips the
  provenance URIs the launch manifest handed it, as ``{type, format?, uris}``
  (``type`` is an open vocabulary; the facade reads no content).
- The facade *minted* those URIs (``utils.makeFileDownloadUrl`` — origin-relative
  ``/<apiRoot>/file/<id>/proxiable/<name>``), so resolving them back to file ids
  is just the facade reading **its own** URL scheme: strict own-scheme validation
  (a URI that does not match the mint is rejected, never dereferenced) plus an
  ACL re-check of each recovered id under the submitting user (the id is
  recoverable from the path by design, so possession is not a capability).
- v1 feeding = b3 (D10): the resolved file ids are forwarded to the CLI as a
  ``<string>`` param; ``slicer_cli_web`` injects ``girderApiUrl``/``girderToken``
  and the CLI fetches + assembles. Grouping/assembly is the client's and the
  CLI's job — the facade stays a pure courier (no DICOM analysis, no SimpleITK).
"""

import copy
import json

from bson.objectid import ObjectId
from girder import logger
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import boundHandler, setRawResponse, setResponseHeader
from girder.constants import AccessType, TokenScope
from girder.exceptions import RestException
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.utility.server import getApiRoot

from ..csrf import csrfProtect
from .slicer_spec import translate_slicer_xml

# ---------------------------------------------------------------------------
# Output-folder ref — provider-owned opaque handle
#
# Job *output* still names its destination folder by a provider-owned ref (a raw
# Girder id, optionally ``girder:folder:<id>``). Every resolution re-loads the
# document with the *user's* WRITE permission — the Girder access check is the
# security boundary. Job *input* resolution no longer uses refs at all; the
# client mints ``{type, uris}`` and the facade reads its own URI scheme below.
# ---------------------------------------------------------------------------

def _stripTypedSourceRef(ref, expectedType):
    """Accept raw ids and optional `girder:<type>:<id>` refs."""
    if not isinstance(ref, str) or not ref:
        raise RestException("Malformed sourceRef")
    prefix = f"girder:{expectedType}:"
    if ref.startswith(prefix):
        return ref[len(prefix):]
    return ref


def resolveSourceRefToFolder(ref, user, level=AccessType.WRITE):
    folderId = _stripTypedSourceRef(ref, "folder")
    folder = Folder().load(folderId, user=user, level=level, exc=True)
    return folder


# ---------------------------------------------------------------------------
# Input URIs → file ids (Seam 1 — the facade reading its own mint)
#
# The client submits each bound input as ``{type, format?, uris}`` where every
# uri is a facade-minted, origin-relative ``/<apiRoot>/file/<id>/proxiable/<name>``
# (``utils.makeFileDownloadUrl``). Resolution recovers the file id from that exact
# shape and nothing else, then re-checks READ access under the submitting user.
# It is type-agnostic: an image, a labelmap (Chunk 15), or any future input all
# resolve through this one path — the facade never branches on ``type``.
# ---------------------------------------------------------------------------

_PROXIABLE_MARKER = "proxiable/"


def _fileIdFromMintedUri(uri):
    """Recover the Girder file id from a facade-minted proxiable uri, or ``None``.

    Mirrors ``utils.makeFileDownloadUrl``: origin-relative
    ``/<apiRoot>/file/<id>/proxiable/<name>``. Parsing keys off ``getApiRoot()``
    (the same root the minter used) rather than a literal ``/api/v1`` prefix, so a
    non-default API mount still resolves. Returns ``None`` for anything that is
    not exactly that shape — a trailing ``proxiable/<non-empty-name>`` with no
    embedded ``/``, ``?`` or ``#`` — so a foreign or malformed string is rejected
    by the caller and never dereferenced.
    """
    if not isinstance(uri, str):
        return None
    prefix = "/" + getApiRoot() + "/file/"
    if not uri.startswith(prefix):
        return None
    fileId, sep, tail = uri[len(prefix):].partition("/")
    if not sep or not ObjectId.is_valid(fileId):
        return None
    if not tail.startswith(_PROXIABLE_MARKER):
        return None
    name = tail[len(_PROXIABLE_MARKER):]
    if not name or "/" in name or "?" in name or "#" in name:
        return None
    return fileId


def resolveInputUrisToFileIds(uris, user):
    """Resolve a client-minted uri list to Girder file ids (fail closed).

    Two obligations (contract Seam 1): (1) **strict own-scheme validation** — a
    uri that does not match the facade's mint is rejected 400 and never fetched;
    and (2) an **ACL re-check** — every recovered id is loaded with the submitting
    user's READ permission, so possession of a (by-design recoverable) id is not
    itself a capability. Validation runs over every uri first, then authorization,
    so a malformed uri fails 400 ahead of an unreadable id's 403.
    """
    if not isinstance(uris, list) or not uris:
        raise RestException("Processing input value carries no uris", code=400)
    fileIds = []
    for uri in uris:
        fileId = _fileIdFromMintedUri(uri)
        if fileId is None:
            raise RestException(
                "Processing input uri does not match this server's file scheme",
                code=400,
            )
        fileIds.append(fileId)
    for fileId in fileIds:
        # The Girder READ permission check is the security boundary; a file the
        # submitting user cannot read raises AccessException (403).
        File().load(fileId, user=user, level=AccessType.READ, exc=True)
    return fileIds


# ---------------------------------------------------------------------------
# Provider config (per-launch payload)
# ---------------------------------------------------------------------------

def _providerBaseUrl(folder):
    return f"/api/v1/folder/{folder['_id']}/volview_processing"


def _providerConfigForFolder(folder, user):
    # No advertised sources: the client mints its own input refs from the
    # on-screen volume's provenance (D10 — grouping moved to the client), so the
    # facade advertises only where to reach the provider, not what is loaded.
    return {
        "id": "girder-slicer-cli",
        "label": "Analysis",
        "protocol": "slicer-cli",
        "baseUrl": _providerBaseUrl(folder),
        "auth": "same-origin",
        "context": {},
    }


def buildProcessingConfigBlock(folder, user):
    return {"providers": [_providerConfigForFolder(folder, user)]}


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
# The *server* is the boundary: ``getTaskXml``/``runTask`` 404 a filtered-out
# taskId exactly like an unknown id, so scoping can't be bypassed by guessing
# an id. Fail-closed: a CLI with no/unknown ``<category>`` is excluded.
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


def _cliCategory(xmlText):
    """Return a CLI's declared ``<category>`` (stripped) or None if absent/bad."""
    if not xmlText:
        return None
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xmlText)
    except ET.ParseError:
        return None
    el = root.find("category")
    if el is None or not el.text:
        return None
    return el.text.strip() or None


def _taskInScope(cliItem, allowed=None):
    """Whether a CLI's ``<category>`` is in the allowed scope (fail-closed).

    A CLI with no/unknown ``<category>`` is excluded so scoping can't be
    bypassed. ``allowed`` (lowercased set) is passed in by ``_scopedCliItems``
    to parse the env once per request; the single-task callers omit it.
    """
    if allowed is None:
        allowed = _allowedCategories()
    try:
        category = _cliCategory(cliItem.xml)
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


# Compound extensions we want to preserve as a single suffix.
_COMPOUND_EXTENSIONS = (
    ".nii.gz", ".tar.gz", ".mgh.gz", ".hdr.gz", ".mnc.gz",
    ".iwi.cbor.zst", ".iwi.cbor",
)


def _splitExt(name):
    """Like os.path.splitext but recognizes radiology compound extensions."""
    lower = name.lower()
    for ext in _COMPOUND_EXTENSIONS:
        if lower.endswith(ext):
            return name[: -len(ext)], name[-len(ext):]
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]


def _defaultExtensionForOutput(out):
    """Pick a sensible extension when the CLI didn't declare one."""
    if out["tag"] == "image":
        return ".nii.gz"
    return ".dat"


def _outputExtension(out):
    """Return the first declared fileExtension, or a tag-based default."""
    raw = out.get("fileExtensions") or ""
    for ext in raw.split(","):
        ext = ext.strip()
        if ext:
            return ext if ext.startswith(".") else "." + ext
    return _defaultExtensionForOutput(out)


def _candidateOutputName(inputBase, cliName, paramName, ext):
    """Build a deterministic candidate name; uniquifying is a separate step."""
    base = (inputBase or "output").strip(". ")
    cli = (cliName or "task").strip(". ")
    return f"{base}.{cli}.{paramName}{ext}"


def _uniquifyItemName(folder, candidate):
    """Append ` (N)` to the base until the name doesn't collide in the folder."""
    base, ext = _splitExt(candidate)
    name = candidate
    suffix = 2
    while Item().findOne({"folderId": folder["_id"], "name": name}) is not None:
        name = f"{base} ({suffix}){ext}"
        suffix += 1
        if suffix > 999:  # safety
            break
    return name


def _firstInputBaseName(values):
    """Base name (no extension) of the first client-minted input, for naming.

    Pure string parse of the input value's first uri (the minted
    ``…/proxiable/<name>`` — its last path segment is the original filename), so
    an auto-generated output reads ``<inputname>.<cli>.<param><ext>``. No file
    load, no ACL — this only seeds a name; the real resolution/validation happens
    in ``_translateValuesToSlicerParams``. Falls back to ``"output"`` when there
    is no usable input uri.
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


def _autofillOutputs(values, cli_xml, cli_name, folder):
    """Auto-generate unique names for any output params the client didn't fill in.

    Mutates and returns `values`. Output param values become
    `ProcessingOutputRequest`-style dicts: `{"name": "<unique>", ...}`.
    """
    outputs = _parseCliOutputs(cli_xml or "")
    if not outputs:
        return values

    inputBase = _firstInputBaseName(values)

    for out in outputs:
        existing = values.get(out["name"])
        if isinstance(existing, dict) and existing.get("name"):
            continue
        ext = _outputExtension(out)
        candidate = _candidateOutputName(inputBase, cli_name, out["name"], ext)
        unique = _uniquifyItemName(folder, candidate)
        new_value = {"name": unique}
        if isinstance(existing, dict):
            new_value.update({k: v for k, v in existing.items() if k != "name"})
        values[out["name"]] = new_value
    return values


def _parseCliOutputs(xmlText):
    """Parse `<image channel=output>` / `<file channel=output>` from XML.

    Returns a list of dicts:
        [{name, tag, isLabel, fileExtensions}]
    Used to pick result files out of Job._original_params after success.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xmlText)
    except ET.ParseError:
        return []
    outputs = []
    for param in root.iter():
        channelEl = param.find("channel")
        if channelEl is None or (channelEl.text or "").strip() != "output":
            continue
        if param.tag not in {"image", "file"}:
            continue
        nameEl = param.find("name")
        if nameEl is None or not nameEl.text:
            continue
        outputs.append({
            "name": nameEl.text.strip(),
            "tag": param.tag,
            "isLabel": param.get("type") == "label",
            "fileExtensions": (param.get("fileExtensions") or "").lower(),
        })
    return outputs


def _intentForOutput(out):
    """Result intent name for an output (item 3.1).

    Emitted additively alongside the legacy ``role`` using the same five-name
    v1 vocabulary the client validates (VolView ``src/processing/intents.ts``):
    a labelmap → ``attach-segment-group`` (mirrors ``role == "segmentGroup"``),
    a plain image → ``add-base-image``, any other file → ``download``. No CLI
    declares a state output yet, so ``restore-state`` has no producer here.
    """
    if out["isLabel"]:
        return "attach-segment-group"
    if out["tag"] == "image":
        return "add-base-image"
    return "download"


def _readLabelsSidecar(fileDoc):
    """Read a small JSON sidecar listing per-label segment descriptors.

    Returns a list like `[{"value": 1, "name": "...", "color": [r,g,b,a]}, ...]`
    or `None` if the file isn't a parseable JSON list of labels.
    """
    if (fileDoc.get("size") or 0) > 256 * 1024:
        return None  # not our sidecar
    try:
        chunks = File().download(fileDoc, headers=False)
        raw = b"".join(chunks() if callable(chunks) else chunks)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    cleaned = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if "value" not in entry or "name" not in entry:
            continue
        color = entry.get("color")
        if not isinstance(color, list) or len(color) not in (3, 4):
            continue
        if len(color) == 3:
            color = list(color) + [255]
        cleaned.append({
            "value": int(entry["value"]),
            "name": str(entry["name"]),
            "color": [int(c) for c in color],
        })
    return cleaned or None


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


def _projectJobStatus(job):
    """Convert Girder Job status to ProcessingJobStatus."""
    from girder_jobs.constants import JobStatus
    state_map = {
        JobStatus.INACTIVE: "pending",
        JobStatus.QUEUED: "pending",
        JobStatus.RUNNING: "running",
        JobStatus.SUCCESS: "success",
        JobStatus.ERROR: "error",
        JobStatus.CANCELED: "cancelled",
    }
    state = state_map.get(job.get("status"), "pending")
    out = {"jobId": str(job["_id"]), "state": state}
    if state == "error":
        log = job.get("log") or []
        if isinstance(log, list):
            tail = "".join(log[-20:])
        else:
            tail = str(log)[-2000:]
        out["errorTail"] = tail
    progress = job.get("progress") or {}
    if progress.get("total") and progress.get("current") is not None:
        try:
            out["progress"] = float(progress["current"]) / float(progress["total"])
        except (TypeError, ZeroDivisionError):
            pass
    return out


def _collectJobResults(job, user):
    """Find result files based on Job._original_params + CLI XML."""
    from slicer_cli_web.models import CLIItem
    results = []
    original_params = job.get("_original_params") or {}
    if not original_params:
        return results

    # Look up the CLI XML from job metadata if possible.
    cli_xml = None
    cli_id = job.get("_original_path")
    # _original_path is a folder restBasePath (e.g. ".../<image>"). Easier:
    # look up CLIItem by name + path.
    name = job.get("_original_name")
    if name:
        from girder_jobs.models.job import Job as JobModel  # noqa: F401
        # Try to find the matching CLIItem by name.
        try:
            for c in CLIItem.findAllItems(user):
                if c.name == name:
                    cli_xml = c.xml
                    break
        except Exception:
            logger.exception("Failed to look up CLIItem for job results")

    outputs = _parseCliOutputs(cli_xml) if cli_xml else []
    if not outputs:
        return results

    # Pass 1: resolve each declared output to its uploaded file document.
    resolved = []
    for out in outputs:
        outName = out["name"]
        if outName not in original_params:
            continue
        fileName = original_params[outName]
        folderId = original_params.get(f"{outName}_folder")
        if not fileName or not folderId:
            continue
        try:
            folder = Folder().load(
                folderId, user=user, level=AccessType.READ, exc=False
            )
            if not folder:
                continue
            item = Item().findOne({
                "folderId": folder["_id"], "name": fileName,
            })
            if not item:
                continue
            fileDoc = next(iter(Item().childFiles(item, limit=1)), None)
            if not fileDoc:
                continue
        except Exception:
            logger.exception("Failed to resolve job output file")
            continue
        resolved.append({"out": out, "fileDoc": fileDoc})

    # Pass 2: find any JSON labels sidecars and pair them with labelmap outputs.
    sidecars = []
    for entry in list(resolved):
        out = entry["out"]
        fileDoc = entry["fileDoc"]
        name = (fileDoc.get("name") or "").lower()
        if out["tag"] == "file" and (
            name.endswith(".json") or ".labels.json" in name
        ):
            labels = _readLabelsSidecar(fileDoc)
            if labels:
                sidecars.append(labels)
                resolved.remove(entry)
    # For now, pair-by-position: a sidecar attaches to the first labelmap output.
    labelmap_entries = [
        e for e in resolved
        if e["out"]["tag"] == "image" and e["out"]["isLabel"]
    ]

    # Pass 3: project each remaining file into ProcessingResult.
    for entry in resolved:
        out = entry["out"]
        fileDoc = entry["fileDoc"]
        role = "segmentGroup" if out["isLabel"] else None
        url = (
            f"/api/v1/file/{fileDoc['_id']}/proxiable/"
            f"{fileDoc['name']}"
        )
        result = {
            "id": str(fileDoc["_id"]),
            "name": fileDoc["name"],
            "url": url,
            # `intent` is additive: `role` stays unchanged for older clients,
            # while intent-aware clients prefer this validated field (item 3.1).
            **({"role": role} if role else {}),
            "intent": _intentForOutput(out),
            "mimeType": fileDoc.get("mimeType"),
            "size": fileDoc.get("size"),
        }
        if (
            role == "segmentGroup"
            and sidecars
            and entry in labelmap_entries
        ):
            # First labelmap gets the first sidecar.
            idx = labelmap_entries.index(entry)
            if idx < len(sidecars):
                result["segments"] = sidecars[idx]
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("List processing tasks available for a folder.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .produces(["application/json"])
)
def listTasks(self, folder):
    user = self.getCurrentUser()
    tasks = []
    if user and _slicerCliAvailable():
        try:
            tasks.extend([_cliItemToSummary(c) for c in _scopedCliItems(user)])
        except Exception:
            logger.exception("Failed to list slicer_cli_web items")
    return tasks


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get the Slicer CLI XML for a task.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("taskId", "The task identifier.", paramType="path")
)
def getTaskXml(self, folder, taskId):
    user = self.getCurrentUser()
    if not _slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=404)
    cliItem = _findScopedCliItem(taskId, user)
    if not cliItem:
        raise RestException("Unknown taskId", code=404)
    # Arm the raw XML response only AFTER the guards. setRawResponse() before a
    # raised RestException makes cherrypy try to encode the error's str body as
    # raw bytes (collapse_body: "expected a bytes-like object, str found"),
    # turning an intended 404 (filtered-out / unknown task) into a 500.
    setResponseHeader("Content-Type", "application/xml")
    setRawResponse()
    return cliItem.xml


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get the VolView task spec for a task.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("taskId", "The task identifier.", paramType="path")
)
def getTaskSpec(self, folder, taskId):
    # Seam 2 (Chunk 6): the facade translates the Slicer XML into VolView's own
    # task spec server-side (D2), so the client never parses backend XML. Runs
    # alongside getTaskXml until the client switches (getTaskXml is removed in
    # Chunk 13). Same scope guards as getTaskXml: an out-of-scope / unknown /
    # slicer_cli_web-missing taskId 404s identically.
    user = self.getCurrentUser()
    if not _slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=404)
    cliItem = _findScopedCliItem(taskId, user)
    if not cliItem:
        raise RestException("Unknown taskId", code=404)
    return translate_slicer_xml(cliItem.xml, cliItem.name)


def _genDockerJob(cliItem, params, user):
    """Create the slicer_cli_web docker job for a CLI item and return its doc.

    Isolated as the single live slicer_cli_web touch point so ``runTask`` (and
    its tests) can drive job creation without the optional dependency.
    """
    from girder.models.token import Token
    from slicer_cli_web.rest_slicer_cli import genHandlerToRunDockerCLI
    handler = genHandlerToRunDockerCLI(cliItem)
    token = Token().createToken(user=user)
    # Take a copy so the handler can mutate freely.
    job_obj = handler.subHandler(cliItem, copy.deepcopy(params), user, token)
    return job_obj.job if hasattr(job_obj, "job") else job_obj


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@csrfProtect
@boundHandler
@autoDescribeRoute(
    Description("Submit a processing task.")
    .modelParam("folderId", model=Folder, level=AccessType.WRITE)
    .param("taskId", "The task identifier.", paramType="path")
    .jsonParam(
        "body",
        "Submission payload: { values: { paramName: ProcessingValue, ... } }",
        paramType="body",
        required=False,
    )
)
def runTask(self, folder, taskId, body):
    user = self.getCurrentUser()
    values = (body or {}).get("values", {}) if isinstance(body, dict) else {}

    if not _slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=500)

    cliItem = _findScopedCliItem(taskId, user)
    if not cliItem:
        raise RestException("Unknown taskId", code=404)

    # Auto-generate (unique) output filenames so the user never has to. Any
    # output param missing from `values` gets a fresh name keyed off the input
    # file + CLI name + parameter name + extension.
    values = _autofillOutputs(dict(values), cliItem.xml, cliItem.name, folder)

    # Resolve each bound input's client-minted URIs back to file ids (own-scheme
    # validation + per-user ACL re-check) and forward the ids to the CLI (b3).
    params = _translateValuesToSlicerParams(values, user, folder)
    logger.info(
        "[volview_processing] runTask folder=%s task=%s params=%s",
        folder["_id"], taskId, params,
    )

    job_doc = _genDockerJob(cliItem, params, user)
    return {"jobId": str(job_doc["_id"])}


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get job status.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJob(self, folder, jobId):
    user = self.getCurrentUser()
    from girder_jobs.models.job import Job as JobModel
    job = JobModel().load(jobId, user=user, level=AccessType.READ, exc=True)
    return _projectJobStatus(job)


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get job results.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJobResults(self, folder, jobId):
    user = self.getCurrentUser()
    from girder_jobs.models.job import Job as JobModel
    job = JobModel().load(jobId, user=user, level=AccessType.READ, exc=True)
    return _collectJobResults(job, user)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def addProcessingRoutes(info):
    info["apiRoot"].folder.route(
        "GET", (":folderId", "volview_processing", "tasks"), listTasks
    )
    info["apiRoot"].folder.route(
        "GET",
        (":folderId", "volview_processing", "tasks", ":taskId", "xml"),
        getTaskXml,
    )
    info["apiRoot"].folder.route(
        "GET",
        (":folderId", "volview_processing", "tasks", ":taskId", "spec"),
        getTaskSpec,
    )
    info["apiRoot"].folder.route(
        "POST",
        (":folderId", "volview_processing", "tasks", ":taskId", "run"),
        runTask,
    )
    info["apiRoot"].folder.route(
        "GET",
        (":folderId", "volview_processing", "jobs", ":jobId"),
        getJob,
    )
    info["apiRoot"].folder.route(
        "GET",
        (":folderId", "volview_processing", "jobs", ":jobId", "results"),
        getJobResults,
    )
