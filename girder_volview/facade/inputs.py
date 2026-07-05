"""Processing facade -- input resolution + transient staging lifecycle (Seam 1).

Split out of the former monolith ``processing.py`` (Chunk 32, pure code motion).
This module owns the two halves of Seam 1:

- **Resolution**: recovering Girder ids from the provenance handles the client
  round-trips -- a job *output*'s destination folder ref
  (``resolveSourceRefToFolder``) and each bound *input*'s facade-minted proxiable
  URIs (``resolveInputUrisToFileIds``). The facade reads its OWN URL scheme;
  strict own-scheme validation plus a per-user ACL re-check are the boundary.
- **Transient staging**: client-held bytes earn provenance through
  ``stageInput`` (the route lives in ``routes.py``); the two cleanup obligations
  (job-end deletion + orphan sweep) live here alongside the launch-context stamp
  a reloaded client re-discovers its jobs by.
"""

import datetime

import cherrypy
from bson.objectid import ObjectId
from girder import logger
from girder.constants import AccessType
from girder.exceptions import RestException
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.upload import Upload
from girder.utility import RequestBodyStream
from girder.utility.server import getApiRoot

# ---------------------------------------------------------------------------
# Output-folder ref -- provider-owned opaque handle
#
# Job *output* still names its destination folder by a provider-owned ref (a raw
# Girder id, optionally ``girder:folder:<id>``). Every resolution re-loads the
# document with the *user's* WRITE permission -- the Girder access check is the
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
# (``stageInput`` in ``routes.py``): the bytes land in a fresh item tagged
# transient and the facade mints its own proxiable download URI for them. From
# that point a staged input is indistinguishable from any other minted input and
# resolves through the exact same own-scheme path (``resolveInputUrisToFileIds``)
# — the facade never branches on ``type``.
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


def _collectInputUris(values):
    """The verbatim input opaque URIs across a submission's bound inputs (Chunk 19).

    A bound input arrives as ``{type, format?, uris}``; its ``uris`` are the
    client-minted proxiable URLs (Seam 1 provenance) — the exact strings the
    reloaded client re-derives from the same launch manifest, so tier-2 can match
    a re-discovered job's inputs against the reloaded scene byte-for-byte. Order
    is preserved and duplicates dropped; type-agnostic (never branches on
    ``type``), so image and labelmap inputs both contribute.
    """
    uris = []
    for value in (values or {}).values():
        if not (isinstance(value, dict) and isinstance(value.get("uris"), list)):
            continue
        for uri in value["uris"]:
            if isinstance(uri, str) and uri not in uris:
                uris.append(uri)
    return uris


# ---------------------------------------------------------------------------
# Launch-context stamp (Chunk 19, D5). Girder jobs are user-owned, not
# folder-linked, and the Chunk-17 output-reference binding adds no job->folder
# link — so `listRecentJobs` can only scope to *this study's* jobs if the launch
# context is recorded ON the job at submit. Plain otherFields (queryable Mongo
# keys), not a schema change. The task id + the job's input opaque URIs ride
# along so `listRecentJobs` can project a `NeutralJobHandle` (jobId + taskId +
# inputUris + finishedAt) without re-deriving them. `_projectJobHandle`
# (results.py) reads `_TASK_ID_FIELD`/`_INPUT_URIS_FIELD`; `listRecentJobs`
# (routes.py) queries `_LAUNCH_FOLDER_FIELD`.
# ---------------------------------------------------------------------------
_LAUNCH_FOLDER_FIELD = "volviewLaunchFolderId"  # str(folder _id) — scope key
_TASK_ID_FIELD = "volviewTaskId"                # str — NeutralJobHandle.taskId
_INPUT_URIS_FIELD = "volviewInputUris"          # [str] — NeutralJobHandle.inputUris


def _stampJobContext(job_doc, folder, taskId, inputUris):
    """Stamp the launch context + handle inputs on the job at submit (Chunk 19, D5).

    Records the launch folder (the ``listRecentJobs`` scope key — Girder jobs are
    user-owned, not folder-linked), the task id, and the job's input opaque URIs,
    all as plain otherFields (queryable, not a schema change). This is what lets a
    reloaded client re-discover *this study's* jobs and re-associate their results
    to the reloaded scene by provenance.
    """
    from girder_jobs.models.job import Job as JobModel
    try:
        JobModel().updateJob(job_doc, otherFields={
            _LAUNCH_FOLDER_FIELD: str(folder["_id"]),
            _TASK_ID_FIELD: str(taskId),
            _INPUT_URIS_FIELD: list(inputUris),
        })
    except Exception:
        logger.exception(
            "Failed to stamp launch context on job %s", job_doc.get("_id")
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


def _liveJobClaimedItemIds():
    """Item ids currently claimed by a non-terminal (live) facade job.

    A job records its staged input item ids in ``volviewTransient`` at submit
    (``_markJobTransients``); only ``_cleanupTransientOnJobDone`` clears them, and
    only at a terminal state. So any such id on a job that has NOT reached a
    terminal state is a live input the orphan sweep must never delete. Unscoped by
    folder on purpose: a job's launch folder equals its staged inputs' folder in
    the normal flow, but the union stays correct even if a future client stages
    into one folder and submits against another. Raises on query failure so the
    caller can fail closed.
    """
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job as JobModel
    terminal = [JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED]
    claimed = set()
    # Project only the claimed-item array: this runs once per staged input, and an
    # unprojected job doc drags its unbounded ``log`` / args / kwargs along for a
    # field we never read.
    cursor = JobModel().find({
        _TRANSIENT_META_KEY: {"$exists": True, "$ne": []},
        "status": {"$nin": terminal},
    }, fields=[_TRANSIENT_META_KEY])
    for job in cursor:
        for itemId in (job.get(_TRANSIENT_META_KEY) or []):
            claimed.add(str(itemId))
    return claimed


def _sweepOrphanTransients(folder, now=None):
    """Age out transient items in ``folder`` never bound to a LIVE job (best-effort).

    Piggybacked on staging calls (an upload precedes its job, so job-end cleanup
    never sees a never-submitted orphan). Keyed off ``item['created']`` because the
    marker carries no timestamp; only items strictly older than
    :data:`_TRANSIENT_ORPHAN_TTL` are candidates, so the item this same call is
    about to create is never one. An item that is still an input of a non-terminal
    job is NOT an orphan -- ``_cleanupTransientOnJobDone`` owns it and deletes it at
    terminal state -- so it is excluded here regardless of age. (A genuinely stuck
    job that never reaches terminal thus leaks its input indefinitely; that bounded
    leak is the deliberate price of never deleting a live job's input.)
    """
    now = now or datetime.datetime.utcnow()
    cutoff = now - _TRANSIENT_ORPHAN_TTL
    # Resolve the live-job claim set FIRST; if that lookup fails, skip the whole
    # sweep rather than risk deleting a live job's staged input (fail closed).
    try:
        claimed = _liveJobClaimedItemIds()
    except Exception:
        logger.exception(
            "Failed to resolve live-job transient claims; skipping orphan sweep"
        )
        return
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
        if str(item["_id"]) in claimed:
            continue  # bound to a live (non-terminal) job -- not an orphan
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
