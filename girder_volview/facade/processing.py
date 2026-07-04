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
import datetime
import json

import cherrypy
from bson.objectid import ObjectId
from girder import events, logger
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import boundHandler
from girder.constants import AccessType, TokenScope
from girder.exceptions import GirderException, RestException
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.upload import Upload
from girder.utility import RequestBodyStream
from girder.utility.server import getApiRoot

from ..csrf import csrfProtect
from ..utils import makeFileDownloadUrl
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
# Transient staging lifecycle (Seam 1 — D10 "Client-created segmentation inputs")
#
# Client-held bytes (a painted labelmap, and — deferred item 10 — any future
# consented upload) earn provenance through the type-agnostic staging endpoint
# (``stageInput`` below): the bytes land in a fresh item tagged transient and the
# facade mints its own proxiable download URI for them. From that point a staged
# input is indistinguishable from any other minted input and resolves through the
# exact same own-scheme path (``resolveInputUrisToFileIds``) — the facade never
# branches on ``type``.
#
# "Transient" is two cleanup obligations, both rebuilt here (the original b1
# cluster was deleted in Chunk 9):
#   1. Submit-side: a resolved input whose item is transient is recorded on its
#      job; ``_cleanupTransientOnJobDone`` (bound to ``jobs.job.update.after``)
#      deletes it once the job reaches a terminal state.
#   2. Orphan sweep: an item uploaded but never submitted has no job to clean it
#      up, so each staging call ages out transient items older than the TTL,
#      keyed off ``item['created']`` (the marker carries no timestamp).
# ---------------------------------------------------------------------------

# Item metadata key marking a staged, non-durable input (invisible to session
# history + source listings; gone at job end or TTL). Nothing labelmap-specific
# rides this tag — it is the whole staging vocabulary.
_TRANSIENT_META_KEY = "volviewTransient"

# Age after which an uploaded-but-never-submitted transient item is swept on the
# next staging call. Upload->submit is normally seconds; a day absorbs an
# interrupted session without cluttering folders across days (Chunk 14 in-flight
# decision — the WORKORDER recommendation, made a module constant).
_TRANSIENT_ORPHAN_TTL = datetime.timedelta(hours=24)


def _isTransientItem(item):
    """Whether an item carries the staging marker."""
    return bool((item or {}).get("meta", {}).get(_TRANSIENT_META_KEY))


def _transientItemIdForFile(fileId, user):
    """The parent item id of ``fileId`` if that item is transient, else ``None``.

    Loaded under the submitting user's READ permission (the file already passed
    the same check in ``resolveInputUrisToFileIds``); a non-transient input, or an
    item that no longer loads, yields ``None`` so only genuinely staged inputs are
    ever recorded for cleanup.
    """
    fileDoc = File().load(fileId, user=user, level=AccessType.READ, exc=False)
    if not fileDoc:
        return None
    itemId = fileDoc.get("itemId")
    if not itemId:
        return None
    item = Item().load(itemId, user=user, level=AccessType.READ, exc=False)
    if item and _isTransientItem(item):
        return str(item["_id"])
    return None


def _collectTransientInputItemIds(values, user):
    """Transient input item ids among a submission's bound inputs (deduped).

    A bound input arrives as ``{type, format?, uris}``; its URIs resolve to file
    ids through the same own-scheme path the CLI params use, and any whose parent
    item is transient (i.e. was staged) is collected so the job can delete it at
    terminal state. Type-agnostic: it never branches on ``type``.
    """
    itemIds = []
    for value in (values or {}).values():
        if not (isinstance(value, dict) and "uris" in value):
            continue
        for fileId in resolveInputUrisToFileIds(value.get("uris"), user):
            transientItemId = _transientItemIdForFile(fileId, user)
            if transientItemId and transientItemId not in itemIds:
                itemIds.append(transientItemId)
    return itemIds


def _markJobTransients(job_doc, transientItemIds):
    """Record transient input items on the job so cleanup can delete them."""
    from girder_jobs.models.job import Job as JobModel
    try:
        JobModel().updateJob(
            job_doc, otherFields={_TRANSIENT_META_KEY: list(transientItemIds)}
        )
    except Exception:
        logger.exception(
            "Failed to mark transient inputs on job %s", job_doc.get("_id")
        )


def _removeTransientItems(itemIds):
    """Delete transient input items by id (idempotent, best-effort)."""
    for itemId in itemIds:
        try:
            item = Item().load(itemId, force=True)
            if item:
                Item().remove(item)
        except Exception:
            logger.exception("Failed to remove transient item %s", itemId)


def _cleanupTransientOnJobDone(event):
    """Delete a job's transient staged inputs once it reaches a terminal state.

    Bound to ``jobs.job.update.after``. Idempotent: a re-fired terminal update
    finds the items already gone and no-ops. The job is reloaded from the database
    before reading the marker/status -- ``updateJob`` fires this event with the
    updater's *in-memory* job dict, which carries the marker only if that updater
    happened to DB-load the job first. Reloading keeps cleanup self-contained: it
    works for any terminal updater (girder_worker, a manual cancel, ...), not just
    this facade's own ``updateJob`` call.
    """
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job as JobModel
    info = getattr(event, "info", None)
    eventJob = info.get("job") if isinstance(info, dict) else None
    if not isinstance(eventJob, dict):
        return
    job = JobModel().load(eventJob.get("_id"), force=True)
    if not isinstance(job, dict):
        return
    transientItemIds = job.get(_TRANSIENT_META_KEY)
    if not isinstance(transientItemIds, list) or not transientItemIds:
        return
    terminal = {JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED}
    if job.get("status") not in terminal:
        return
    _removeTransientItems(transientItemIds)


def _sweepOrphanTransients(folder, now=None):
    """Age out transient items in ``folder`` never bound to a job (best-effort).

    Piggybacked on staging calls (an upload precedes its job, so job-end cleanup
    never sees an orphan). Keyed off ``item['created']`` because the marker carries
    no timestamp; only items strictly older than :data:`_TRANSIENT_ORPHAN_TTL` are
    swept, so the item this same call is about to create is never a candidate.
    """
    now = now or datetime.datetime.utcnow()
    cutoff = now - _TRANSIENT_ORPHAN_TTL
    query = {
        "folderId": folder["_id"],
        "meta.%s" % _TRANSIENT_META_KEY: True,
        "created": {"$lt": cutoff},
    }
    try:
        stale = list(Item().find(query))
    except Exception:
        logger.exception("Failed to query orphan transient items")
        return
    for item in stale:
        try:
            Item().remove(item)
        except Exception:
            logger.exception(
                "Failed to sweep orphan transient item %s", item.get("_id")
            )


def _streamBodyIntoItem(folder, user, size, name):
    """Stream the raw request body into a new item in ``folder``.

    Mirrors ``__init__.uploadSession``'s streaming dance -- ``createUpload`` + a
    raw ``RequestBodyStream`` chunk + ``handleChunk``/``finalizeUpload`` -- but
    stays format-blind: a caller-supplied ``name`` and an
    ``application/octet-stream`` mime, nothing session- or labelmap-specific.
    Returns the finalized, filtered file document.
    """
    parent = Folder().load(
        id=folder["_id"], user=user, level=AccessType.WRITE, exc=True
    )
    chunk = None
    ct = cherrypy.request.body.content_type.value
    if (
        ct not in cherrypy.request.body.processors
        and ct.split("/", 1)[0] not in cherrypy.request.body.processors
    ):
        chunk = RequestBodyStream(cherrypy.request.body)
    if chunk is not None and chunk.getSize() <= 0:
        chunk = None
    upload = Upload().createUpload(
        user=user,
        name=name,
        parentType="folder",
        parent=parent,
        size=size,
        mimeType="application/octet-stream",
        reference=None,
    )
    if chunk:
        return Upload().handleChunk(upload, chunk, filter=True, user=user)
    return File().filter(Upload().finalizeUpload(upload), user)


def _tagItemTransient(fileDoc, user):
    """Tag a freshly-uploaded file's parent item transient; return the item."""
    itemId = fileDoc.get("itemId")
    if not itemId:
        return None
    item = Item().load(itemId, force=True)
    if item:
        Item().setMetadata(item, {_TRANSIENT_META_KEY: True})
    return item


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
# The *server* is the boundary: ``getTaskSpec``/``runTask`` 404 a filtered-out
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


def _autofillOutputs(values, cli_xml, cli_name):
    """Auto-generate a deterministic name for output params the client didn't fill.

    Mutates and returns `values`. Output param values become
    `ProcessingOutputRequest`-style dicts: `{"name": "<candidate>", ...}`.

    The name is deterministic (`<input>.<cli>.<param><ext>`) and NOT uniquified:
    the old check-then-use `while findOne(name)` folder scan was itself racy (two
    concurrent submits both saw a name free and both took it) and is now needless —
    outputs bind to the job by reference (`_recordJobOutput`), never by filename, so
    two jobs writing the same name into one folder no longer cross results (D5).
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
        new_value = {"name": candidate}
        if isinstance(existing, dict):
            new_value.update({k: v for k, v in existing.items() if k != "name"})
        values[out["name"]] = new_value
    return values


def _parseCliOutputs(xmlText):
    """Parse `<image channel=output>` / `<file channel=output>` from XML.

    Returns a list of dicts:
        [{name, tag, isLabel, fileExtensions}]
    Recorded on the job at submit (`_bindJobOutputs`) as the declared output
    descriptors result collection projects each reference-bound file through.
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


def _intentForOutput(out, url, name, jobId, segments=None):
    """Build the declarative result intent for one output (contract Seam 2, D3).

    Results cross the wire as declarative intents the client's single applier
    applies — never a ``role`` the client switches on. The v1 vocabulary the
    client validates (VolView ``processing-contract/wire.ts``): a labelmap →
    ``add-segment-group``, a plain image → ``add-base-image``, any other file →
    ``download``. No CLI declares a state output yet, so ``restore-state`` has no
    producer here.

    A labelmap intent carries a ``source: {jobId, outputId}`` provenance tag
    (``outputId`` = the CLI's output identifier) so the created segment group
    round-trips the ``.volview.zip`` (tier-2 idempotency key, Chunk 19). When the
    bare-labelmap ``segments`` sidecar was folded in it is carried as the
    optional ``segments`` payload; a ``seg.nrrd`` with embedded metadata carries
    none and the client falls back to the file's own metadata. The returned
    object validates against the contract ``result-intent`` schema.
    """
    fileRef = {"url": url, "name": name}
    if out["isLabel"]:
        intent = {
            "intent": "add-segment-group",
            **fileRef,
            "source": {"jobId": str(jobId), "outputId": out["name"]},
        }
        if segments:
            intent["segments"] = segments
        return intent
    if out["tag"] == "image":
        return {"intent": "add-base-image", **fileRef}
    return {"intent": "download", **fileRef}


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


# ---------------------------------------------------------------------------
# Reference-bound job outputs (D5) — outputs bind to the job by reference, never
# by filename
#
# The old collector name-scanned the launch folder for an item matching the
# output filename recorded in ``Job._original_params`` — so two concurrent jobs
# writing the same output name into the same folder could cross results, and two
# simultaneous "unique name" picks could collide. This replaces name-matching
# with the ecosystem's reference→job binding (slicer_cli_web ``girder_plugin.py``
# ``_onUpload`` prior art, generalized to N outputs):
#
#   * At submit, ``_bindJobOutputs`` records on the job (plain ``otherFields`` — no
#     schema change) the declared output specs and the job's own token.
#   * Each output file ``girder_worker`` uploads carries slicer_cli_web's per-run
#     reference (``prepare_task.py`` stamps the output ``identifier`` + a per-run
#     ``uuid`` on every ``GirderUploadVolumePathToFolder`` hook). The ``data.process``
#     handler ``_recordJobOutput`` correlates that upload back to THIS job — by the
#     job's token, which ``girder_worker`` uploads under — and records the file id
#     keyed by ``identifier`` (dotted ``$set`` key → each output binds under its own
#     key, so N outputs never overwrite, unlike slicer_cli_web's single
#     ``slicerCLIBindings.outputs.parameters``).
#   * ``_collectJobResults`` reads those ids OFF the job. No name is ever matched,
#     so the race is gone.
# ---------------------------------------------------------------------------

# Facade-owned job fields (otherFields, not a schema change — D5). The id map is
# READ-exposed (``addProcessingRoutes``); the job's own ACL is the gate.
_OUTPUTS_FIELD = "volviewOutputs"          # {identifier: str(fileId)}
_OUTPUT_SPECS_FIELD = "volviewOutputSpecs"  # [{name, tag, isLabel, fileExtensions}]
_JOB_TOKEN_FIELD = "volviewJobToken"       # str(token _id) — data.process key


def _parseOutputReference(raw):
    """Decode a ``data.process`` upload reference to a dict, or ``None``.

    ``girder_worker`` stamps each output upload with slicer_cli_web's JSON
    reference (``prepare_task.py`` — carries the output ``identifier``). A
    non-JSON / non-dict / identifier-less reference (a foreign upload, or the
    facade's own reference-less staging upload) yields ``None`` so the handler
    skips it (fail closed). An identifier carrying ``.`` or ``$`` is rejected too,
    so it can never be used to build an unintended nested / operator ``$set`` key.
    """
    if raw is None:
        return None
    try:
        ref = raw if isinstance(raw, dict) else json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(ref, dict):
        return None
    identifier = ref.get("identifier")
    if not isinstance(identifier, str) or not identifier:
        return None
    if "." in identifier or "$" in identifier:
        return None
    return ref


def _jobForOutputUpload(ref, info):
    """Find the job an output upload belongs to — reference-bound, never by name.

    Primary correlation is the job's own token: ``_bindJobOutputs`` records
    ``token._id`` on the job at submit, and ``girder_worker`` uploads each output
    under that same job token, so the ``data.process`` event's ``currentToken``
    identifies exactly one job (per-job token → no cross-job collision). A
    facade-minted reference may also carry the job id directly; it is honored when
    present. Returns the job doc, or ``None`` (fail closed — an uncorrelated upload
    is never recorded onto some other job).
    """
    from girder_jobs.models.job import Job as JobModel
    jobId = ref.get("jobId")
    if jobId:
        # A malformed id must never escape and disrupt the data.process daemon.
        try:
            job = JobModel().load(jobId, force=True, exc=False)
        except Exception:
            job = None
        if isinstance(job, dict):
            return job
    token = info.get("currentToken")
    tokenId = token.get("_id") if isinstance(token, dict) else None
    if tokenId:
        return JobModel().findOne({_JOB_TOKEN_FIELD: str(tokenId)})
    return None


def _recordJobOutput(event):
    """``data.process`` handler: record an uploaded output file id onto its job (D5).

    Each output file ``girder_worker`` uploads for a facade job carries
    slicer_cli_web's reference (the output ``identifier``); this handler correlates
    the upload back to the originating job (``_jobForOutputUpload``) and records the
    file id under that identifier (``otherFields`` dotted key → Mongo ``$set`` nests
    per identifier, so N outputs each bind without overwriting). Fail closed: a
    foreign or uncorrelated upload is ignored. ``_collectJobResults`` later reads
    these ids OFF the job, so no output is ever resolved by folder-name match.
    """
    info = getattr(event, "info", None)
    if not isinstance(info, dict):
        return
    ref = _parseOutputReference(info.get("reference"))
    if ref is None:
        return
    fileDoc = info.get("file")
    fileId = fileDoc.get("_id") if isinstance(fileDoc, dict) else None
    if not fileId:
        return
    job = _jobForOutputUpload(ref, info)
    if not isinstance(job, dict):
        return
    from girder_jobs.models.job import Job as JobModel
    try:
        JobModel().updateJob(
            job,
            otherFields={"%s.%s" % (_OUTPUTS_FIELD, ref["identifier"]): str(fileId)},
        )
    except Exception:
        logger.exception(
            "Failed to record output %s on job %s",
            ref.get("identifier"), job.get("_id"),
        )


def _bindJobOutputs(job, token, cli_xml):
    """Record on the job everything reference-bound collection needs (D5).

    The declared output specs (so collection needs no CLIItem lookup and no
    ``_original_params``), the job's own token (the ``data.process`` correlation
    key), and an empty id map the handler fills in. Split out from ``_genDockerJob``
    so it is unit-testable without slicer_cli_web: a pure ``updateJob`` write, the
    same otherFields-on-job pattern ``_markJobTransients`` uses. Not a schema change.
    """
    from girder_jobs.models.job import Job as JobModel
    specs = _parseCliOutputs(cli_xml or "")
    try:
        JobModel().updateJob(job, otherFields={
            _OUTPUT_SPECS_FIELD: specs,
            _JOB_TOKEN_FIELD: str(token["_id"]),
            _OUTPUTS_FIELD: {},
        })
    except Exception:
        logger.exception("Failed to bind outputs on job %s", job.get("_id"))


def _recordedJobOutputs(job):
    """The ``{identifier: fileId}`` map the ``data.process`` handler recorded (or {})."""
    outputs = (job or {}).get(_OUTPUTS_FIELD)
    return dict(outputs) if isinstance(outputs, dict) else {}


def _recordedOutputSpecs(job):
    """The declared output specs recorded at submit (or [])."""
    specs = (job or {}).get(_OUTPUT_SPECS_FIELD)
    return list(specs) if isinstance(specs, list) else []


def _loadOutputFile(fileId, user):
    """Load a recorded output file under the user's READ permission, or ``None``.

    Fail closed: a deleted file (gone) or one the user cannot read both yield
    ``None`` so collection counts them as ``missing`` rather than crashing or
    leaking. The submitting user owns the launch folder outputs land in, so a
    genuine READ denial is not expected; it is handled defensively.
    """
    from girder.exceptions import AccessException
    try:
        return File().load(fileId, user=user, level=AccessType.READ, exc=False)
    except AccessException:
        return None


def _collectJobResults(job, user):
    """Resolve a job's outputs to declarative result intents — reference-bound (D5).

    Reads the ``{identifier: fileId}`` map ``_recordJobOutput`` recorded ON the job
    (never a folder-name scan, never ``_original_params``), loads each file under
    the submitting user's READ permission, and projects it into its result intent
    (contract Seam 2) with a ``makeFileDownloadUrl`` download url (origin-relative
    and filename-encoded — retiring the hand-built ``/api/v1/file/…`` f-string that
    broke non-default API mounts). Returns ``(results, missing)`` where ``missing``
    counts recorded outputs whose file is gone/unreadable — a deleted output is a
    countable loss, never a silently shorter list. Two concurrent same-name jobs
    can never cross results: each reads only the ids bound to itself.
    """
    recorded = _recordedJobOutputs(job)
    if not recorded:
        return [], 0
    specByName = {
        s["name"]: s
        for s in _recordedOutputSpecs(job)
        if isinstance(s, dict) and s.get("name")
    }

    # Pass 1: resolve each recorded output id to its file document (ACL re-check).
    resolved = []
    missing = 0
    for identifier, fileId in recorded.items():
        out = specByName.get(identifier)
        if out is None:
            # An identifier with no declared image/file spec (e.g. slicer's
            # returnparameterfile) is not a projectable output; skip, not missing.
            continue
        fileDoc = _loadOutputFile(fileId, user)
        if fileDoc is None:
            missing += 1
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

    # Pass 3: project each remaining file into its declarative result intent
    # (contract Seam 2). The wire shape is the intent object itself — `{intent,
    # url, name, segments?, source?}` — plus the `id`/`mimeType`/`size` file
    # metadata the client's JobList reads. No `role`: the client applies the
    # intent directly and never switches on a role (D3/D4).
    results = []
    for entry in resolved:
        out = entry["out"]
        fileDoc = entry["fileDoc"]
        url = makeFileDownloadUrl(fileDoc)
        # Fold the labels sidecar into the labelmap intent's `segments` payload
        # so the client never learns the sidecar convention.
        segments = None
        if out["isLabel"] and sidecars and entry in labelmap_entries:
            idx = labelmap_entries.index(entry)
            if idx < len(sidecars):
                segments = sidecars[idx]
        intent = _intentForOutput(
            out, url, fileDoc["name"], job["_id"], segments
        )
        result = {
            **intent,
            "id": str(fileDoc["_id"]),
            "mimeType": fileDoc.get("mimeType"),
            "size": fileDoc.get("size"),
        }
        results.append(result)
    return results, missing


def _jobResultsPayload(job, user):
    """Apply honest result-read semantics (D5) and return the wire result list.

    - A non-succeeded job (failed / running / pending / cancelled) is an EXPLICIT
      error, never an empty list — the client (Chunk 12) gates reads on terminal
      success and treats this error as an error, not empty results.
    - A succeeded job whose recorded outputs ALL fail to resolve (every output file
      deleted) is likewise an error carrying the ``missing`` count, so "succeeded,
      outputs deleted" is distinguishable from "succeeded, no outputs".
    - Otherwise the resolved intents are returned as a bare list (wire-unchanged,
      client-transparent); a partial loss returns what resolved and logs the rest.

    ``400`` (not ``404``/``401``) so the client classifies it as a permanent error
    that surfaces loudly without dropping the job or expiring the session.
    """
    state = _projectJobStatus(job).get("state")
    if state != "success":
        raise RestException(
            "Job %s has not succeeded (state=%s); results are unavailable"
            % (job.get("_id"), state),
            code=400,
        )
    results, missing = _collectJobResults(job, user)
    if missing:
        logger.info(
            "[volview_processing] job %s: %d recorded output(s) unresolved",
            job.get("_id"), missing,
        )
    if missing and not results:
        raise RestException(
            "Job %s succeeded but none of its %d recorded output(s) could be "
            "resolved (deleted?)" % (job.get("_id"), missing),
            code=400,
        )
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
    Description("Get the VolView task spec for a task.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("taskId", "The task identifier.", paramType="path")
)
def getTaskSpec(self, folder, taskId):
    # Seam 2 (Chunk 6): the facade translates the Slicer XML into VolView's own
    # task spec server-side (D2), so the client never parses backend XML. Scope
    # guards: an out-of-scope / unknown / slicer_cli_web-missing taskId 404s.
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
    job = job_obj.job if hasattr(job_obj, "job") else job_obj
    # Reference-bound outputs (D5): record the declared output specs + this job's
    # token so the data.process handler can correlate each uploaded output back to
    # THIS job (by token) and record its file id keyed by output identifier.
    _bindJobOutputs(job, token, cliItem.xml)
    return job


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

    # Auto-generate a deterministic output filename for any output param the user
    # didn't fill (input file + CLI name + parameter name + extension). No longer
    # uniquified via a folder scan: outputs bind to the job by reference, not name
    # (D5), so a duplicate filename in the folder can no longer cross results.
    values = _autofillOutputs(dict(values), cliItem.xml, cliItem.name)

    # Resolve each bound input's client-minted URIs back to file ids (own-scheme
    # validation + per-user ACL re-check) and forward the ids to the CLI (b3).
    params = _translateValuesToSlicerParams(values, user, folder)
    # A bound input that was staged (its item tagged transient) is recorded on the
    # job so _cleanupTransientOnJobDone deletes it at terminal state (Chunk 14).
    transientItemIds = _collectTransientInputItemIds(values, user)
    logger.info(
        "[volview_processing] runTask folder=%s task=%s params=%s",
        folder["_id"], taskId, params,
    )

    job_doc = _genDockerJob(cliItem, params, user)
    if transientItemIds:
        _markJobTransients(job_doc, transientItemIds)
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
    return _jobResultsPayload(job, user)


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@csrfProtect
@boundHandler
@autoDescribeRoute(
    Description("Stage client-held bytes as a transient processing input.")
    .notes(
        "Type-agnostic: accepts arbitrary bytes in the request body and returns a "
        "facade-minted download URI for them (the same helper the launch manifest "
        "uses). The created item is tagged transient -- invisible to session "
        "history and source listings, deleted when a job that binds it reaches a "
        "terminal state, or swept if never submitted. Launch-context scoped to the "
        "folder; each call also sweeps this folder's expired orphans."
    )
    .modelParam("folderId", model=Folder, level=AccessType.WRITE)
    .param("name", "File name to record for the staged bytes.", required=False)
    .errorResponse()
)
def stageInput(self, folder, name):
    user = self.getCurrentUser()
    # Age out any never-submitted orphans in this folder before adding another
    # (job-end cleanup never sees an upload that was never submitted).
    _sweepOrphanTransients(folder)
    size = int(cherrypy.request.headers.get("Content-Length") or 0)
    if size <= 0:
        raise GirderException(
            "Expected non-zero Content-Length header",
            "girder.api.v1.folder.volview_stage",
        )
    fileDoc = _streamBodyIntoItem(folder, user, size, name or "staged")
    _tagItemTransient(fileDoc, user)
    # The facade mints the URI; the client constructs none. Type-agnostic shape:
    # the semantic `type` tag is the client's to add at submit, not ours.
    return {"uris": [makeFileDownloadUrl(fileDoc)]}


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def addProcessingRoutes(info):
    # Delete a job's transient staged inputs once it reaches a terminal state
    # (Chunk 14). Bound once at plugin load; fires for every job update but no-ops
    # cheaply unless the job carries the transient marker.
    events.bind(
        "jobs.job.update.after",
        "girder_volview.processing",
        _cleanupTransientOnJobDone,
    )
    # Reference-bound job outputs (D5): record each uploaded output file's id onto
    # its originating job, keyed by output identifier, so result collection reads
    # ids OFF the job instead of scanning the launch folder by name. Fires for
    # every upload but returns early unless the upload carries an output reference
    # correlated to a facade job (fail closed).
    events.bind(
        "data.process",
        "girder_volview.processing.outputs",
        _recordJobOutput,
    )
    # The recorded id map is READ-exposed; the job's own ACL is the gate (D5 —
    # otherFields + exposeFields, mirroring slicer_cli_web's slicerCLIBindings).
    from girder_jobs.models.job import Job as JobModel
    JobModel().exposeFields(level=AccessType.READ, fields={_OUTPUTS_FIELD})
    info["apiRoot"].folder.route(
        "GET", (":folderId", "volview_processing", "tasks"), listTasks
    )
    info["apiRoot"].folder.route(
        "POST", (":folderId", "volview_processing", "stage"), stageInput
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
