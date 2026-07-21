"""Processing backend -- input resolution + transient staging lifecycle."""

import datetime

from bson.objectid import ObjectId
from girder import logger
from girder.constants import AccessType
from girder.exceptions import AccessException, RestException
from girder.models.file import File
from girder.models.item import Item
from girder.models.upload import Upload

# Module-object import (not ``from ... import Job``): call sites resolve
# ``girder_job.Job`` at call time, so tests may monkeypatch the class on
# ``girder_jobs.models.job`` and be seen here.
from girder_jobs.models import job as girder_job

from ..handles import parseFileHandle
from ..utils import TRANSIENT_STAGED_META_KEY, isTransientStagedItem

# Every submitted uri is a backend-minted, origin-relative
# ``/<apiRoot>/file/<id>/proxiable/<name>`` (``utils.makeFileDownloadUrl``).
# Resolution recovers the file id from that exact shape and nothing else, then
# re-checks READ access under the submitting user. Type-agnostic: every input
# resolves through this one path — the backend never branches on ``type``.


def _fileIdFromMintedUri(uri):
    """Recover the Girder file id from a backend-minted proxiable uri, or ``None``.

    Delegates to :func:`girder_volview.handles.parseFileHandle`, the ONE parse
    site for the load-handle scheme and the exact mirror of the mint
    (``handles.mintFileHandle`` / ``utils.makeFileDownloadUrl``). Returns
    ``None`` for anything outside the backend's own scheme, so a foreign or
    malformed string is rejected by the caller and never dereferenced.
    """
    parsed = parseFileHandle(uri)
    return parsed[0] if parsed else None


def resolveInputUrisToFiles(uris, user):
    """Resolve a client-minted uri list to readable Girder files (fail closed).

    A uri that does not match the backend's mint is rejected 400 and never
    fetched, and every recovered id is loaded with the submitting user's READ
    permission, so possession of a (by-design recoverable) id is not itself a
    capability. Validation runs over every uri first, then authorization, so a
    malformed uri fails 400 ahead of an unreadable id's 403.
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
    return _readableFilesInOrder(fileIds, user)


def readableFilesById(fileObjectIds, user, fields=None):
    """Load the file docs whose parent item ``user`` can READ, batched.

    THE one ACL boundary for bulk file reads: a file is readable iff its parent
    item is READable. Girder files inherit access through their parent item, and
    ``File().load`` with a user+level runs a per-file ACL that falls back to
    loading that parent -- ~2 Mongo queries EACH (≈600 for a 300-slice DICOM
    series). Batched instead: one ``File().find`` for every id, then ONE
    permission-filtered ``Item().findWithPermissions`` over the distinct parent
    items. Returns ``{str(fileId): fileDoc}``; a missing file, missing parent,
    or unreadable parent is simply absent, and lenient/strict handling stays
    with the callers.
    """
    fileDocs = list(
        File().find(query={"_id": {"$in": list(fileObjectIds)}}, fields=fields)
    )
    itemIds = {fileDoc.get("itemId") for fileDoc in fileDocs}
    itemIds.discard(None)
    if not itemIds:
        return {}
    readableItemIds = {
        itemDoc["_id"]
        for itemDoc in Item().findWithPermissions(
            query={"_id": {"$in": list(itemIds)}},
            fields={"_id": 1},
            user=user,
            level=AccessType.READ,
        )
    }
    return {
        str(fileDoc["_id"]): fileDoc
        for fileDoc in fileDocs
        if fileDoc.get("itemId") in readableItemIds
    }


def _readableFilesInOrder(fileIds, user):
    """Load READ-authorized file docs for ``fileIds``, in input order, raising.

    A strict adapter over :func:`readableFilesById`: any unreadable id raises
    ``AccessException``. ``_fileIdFromMintedUri`` already validated each id's
    shape, so the ``ObjectId`` conversion cannot fail here. Ordering matches the
    input ``fileIds`` (a comma-joined multi-file volume forwards ids
    positionally), and a repeated id resolves to the same doc.
    """
    filesById = readableFilesById([ObjectId(fileId) for fileId in fileIds], user)
    files = []
    for fileId in fileIds:
        fileDoc = filesById.get(fileId)
        if fileDoc is None:
            # Missing file, missing parent, or a parent the user cannot READ:
            # possession of a (by-design recoverable) id is not a capability.
            raise AccessException("Read access denied for file %s." % fileId)
        files.append(fileDoc)
    return files


def validateStagedDescriptor(descriptor, user):
    """Validate a staged-resource descriptor end to end; return its name.

    Owns the whole descriptor schema — shape, the ``labelmap`` type
    discriminator, and the reference image (own-scheme + ACL + durable) — so the
    ``stageInput`` route stays transport + authorization only and a future
    staged type extends this one validator.
    """
    if set(descriptor) != {"type", "name", "referenceImage"}:
        raise RestException("Malformed staged resource descriptor", code=400)
    if descriptor.get("type") != "labelmap":
        raise RestException("Staged resource type must be labelmap", code=400)
    name = descriptor.get("name")
    if not isinstance(name, str) or not name:
        raise RestException("Staged resource name must not be empty", code=400)
    referenceImage = descriptor.get("referenceImage")
    if not isinstance(referenceImage, dict):
        raise RestException("Staged labelmap requires a reference image", code=400)
    if not set(referenceImage).issubset({"type", "format", "uris"}):
        raise RestException("Malformed staged reference image", code=400)
    if "format" in referenceImage and not isinstance(referenceImage["format"], str):
        raise RestException("Malformed staged reference image format", code=400)
    validateStagedReferenceImage(referenceImage, user)
    return name


def validateStagedReferenceImage(referenceImage, user):
    """Validate a staged labelmap's reference image (own-scheme + ACL + durable).

    Resolves the reference image's own-scheme uris to files under the caller's
    READ permission — rejecting a malformed, foreign, or unauthorized reference
    — and rejects a transient reference so a staged labelmap never binds to
    ephemeral data. Validation only: no lineage is tracked.
    """
    if not isinstance(referenceImage, dict) or referenceImage.get("type") != "image":
        raise RestException("Staged labelmap requires a reference image", code=400)
    fileDocs = resolveInputUrisToFiles(referenceImage.get("uris"), user)
    itemIds = {fileDoc.get("itemId") for fileDoc in fileDocs}
    itemIds.discard(None)
    # ``resolveInputUrisToFiles`` already enforced READ on each file's parent item,
    # so this read only inspects the transient marker: one batched find over the
    # DISTINCT parent items replaces a per-file (and per-duplicate) ``Item().load``.
    for item in Item().find(query={"_id": {"$in": list(itemIds)}}):
        if _isTransientItem(item):
            raise RestException(
                "Staged labelmap requires a durable reference image", code=400
            )


# ``stageInput`` (in ``routes.py``) lands client-held bytes in a fresh item
# tagged transient and mints a proxiable download URI for them; from there a
# staged input resolves through the same own-scheme path as any other input.
# Ownership is per-job by construction (``copyStagedInputsIntoJobFolder``), so
# no job references a shared staged original. Cleanup is therefore split: the
# job deletes its own copies at terminal state, and the TTL sweep below ages
# out the originals, which have no job to clean them up.

# Age after which an uploaded-but-never-submitted transient item is swept on the
# next staging call. Upload->submit is normally seconds; a day absorbs an
# interrupted session without cluttering folders across days.
_TRANSIENT_ORPHAN_TTL = datetime.timedelta(hours=24)


# One definition of the staging predicate, shared with the launch-manifest
# exclusion (``utils.isTransientStagedFile``); aliased so this module's call
# sites and test doubles keep their name.
_isTransientItem = isTransientStagedItem


def copyStagedInputsIntoJobFolder(params, resolvedInputFiles, user, outputFolder):
    """Give the job its OWN copies of any staged (transient) inputs.

    Every transient staged item among a submission's bound inputs is copied
    into the job's private folder and the CLI file-id params are rewritten
    onto the copies, so the job references only resources it alone owns and two
    jobs reusing one staged original can never delete each other's inputs. The
    original stays covered by the TTL orphan sweep. Transience is decided by
    the parent item's marker, never by ``type``.

    URI resolution (``resolveInputUrisToFiles``) already enforced READ on every
    parent item moments ago, so the transient markers are read with ONE batched
    find over the distinct parents rather than a per-item ACL'd load. A parent
    that vanished in between (an orphan sweep or delete) raises 409 rather than
    publishing a job against deleted file ids. ``Item().copyItem`` deep-copies
    metadata, so a copy carries the transient marker and is cleaned up exactly
    like any staged item.

    Returns ``(params, copiedItemIds)`` — the (possibly rewritten) params and
    the copied item ids to record on the job for terminal cleanup.
    """
    itemIds = {
        fileDoc["itemId"]
        for fileDocs in resolvedInputFiles.values()
        for fileDoc in fileDocs
        if (fileDoc or {}).get("itemId")
    }
    items = list(Item().find({"_id": {"$in": list(itemIds)}})) if itemIds else []
    if len(items) != len(itemIds):
        # URI resolution ACL-loaded these parents moments ago, so a missing item
        # means a concurrent delete won the race. Fail the submit rather than
        # publish a job whose params reference deleted files.
        raise RestException(
            "A processing input was removed while the submission "
            "was in progress; please resubmit",
            code=409,
        )
    fileIdRemap = {}
    copiedItemIds = []
    for item in items:
        if not _isTransientItem(item):
            continue
        copied = Item().copyItem(item, creator=user, folder=outputFolder)
        copiedItemIds.append(str(copied["_id"]))
        # Copied files preserve name/size/checksum; sorting both sides by that
        # triple pairs each original with its copy regardless of the underlying
        # cursor order. Girder permits same-named files in one item, so name
        # alone could pair A with B's copy — with the full triple, files that
        # still tie are byte-identical and any pairing is correct. copyItem
        # duplicates every child file, so a length mismatch means the staged
        # item's files changed between resolution and copy — the same race as
        # the concurrent-delete guard above, and the same typed 409 rather than
        # running the job against a partial input.
        def pairingKey(f):
            return (f.get("name", ""), f.get("size", 0), f.get("sha512") or "")

        originals = sorted(Item().childFiles(item), key=pairingKey)
        copies = sorted(Item().childFiles(copied), key=pairingKey)
        if len(originals) != len(copies):
            raise RestException(
                "A processing input changed while the submission "
                "was in progress; please resubmit",
                code=409,
            )
        fileIdRemap.update(
            {
                str(orig["_id"]): str(cop["_id"])
                for orig, cop in zip(originals, copies, strict=True)
            }
        )
    if not fileIdRemap:
        return params, copiedItemIds
    params = dict(params)
    for paramName, fileDocs in resolvedInputFiles.items():
        params[paramName] = ",".join(
            fileIdRemap.get(str(fileDoc["_id"]), str(fileDoc["_id"]))
            for fileDoc in fileDocs
        )
    return params, copiedItemIds


# Girder jobs are user-owned, not folder-linked, so `listJobHistory` can only
# scope to a launch folder if the context is stamped ON the job at submit, as
# plain otherFields (queryable Mongo keys). The task id is backend association
# data the history summary does not expose.
_LAUNCH_FOLDER_FIELD = "volviewLaunchFolderId"  # str(folder _id) — scope key
_TASK_ID_FIELD = "volviewTaskId"


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
    finds the items already gone and no-ops. A present, non-terminal in-memory
    status short-circuits before any DB work, so the common progress/log tick
    costs nothing. When the in-memory status is terminal or absent, the job is
    reloaded from the database before reading the marker/status -- the event
    carries the updater's *in-memory* job dict, which holds the marker only if
    that updater happened to DB-load the job first. Reloading keeps cleanup
    self-contained for any terminal updater (girder_worker, a manual cancel).
    """

    from .results import isTerminalStatus

    info = getattr(event, "info", None)
    eventJob = info.get("job") if isinstance(info, dict) else None
    if not isinstance(eventJob, dict):
        return
    # This handler fires on EVERY job update instance-wide (progress ticks, log
    # appends). ``updateJob`` sets the new status ON the in-memory job dict
    # before firing, so a present, non-terminal in-memory status is an
    # authoritative "not settled yet" -- short-circuit before the DB reload the
    # steady-state stream would otherwise pay on every tick.
    inMemoryStatus = eventJob.get("status")
    if inMemoryStatus is not None and not isTerminalStatus(inMemoryStatus):
        return
    # Reload the committed doc to read the marker/status self-containedly -- the
    # event's in-memory job dict may carry neither. includeLog=False because
    # only the marker + status are read; loading the unbounded log would
    # re-materialize it out of Mongo on every tick.
    job = girder_job.Job().load(eventJob.get("_id"), force=True, includeLog=False)
    if not isinstance(job, dict):
        return
    transientItemIds = job.get(TRANSIENT_STAGED_META_KEY)
    if not isinstance(transientItemIds, list) or not transientItemIds:
        return
    if not isTerminalStatus(job.get("status")):
        return
    _removeTransientItems(transientItemIds)


def _sweepOrphanTransients(folder, now=None):
    """Age out stale transient items in ``folder`` (best-effort).

    Piggybacked on staging calls (an upload precedes its job, so job-end cleanup
    never sees a never-submitted orphan). Keyed off ``item['created']`` because
    the marker carries no timestamp; only items strictly older than
    :data:`_TRANSIENT_ORPHAN_TTL` are candidates, so the item this same call is
    about to create is never one. Age alone decides: no job ever depends on a
    staged ORIGINAL — submission rewires the job onto its own private copies
    (:func:`copyStagedInputsIntoJobFolder`), which live in the job's private
    folder, not the staging folder this sweep scans.
    """
    now = now or datetime.datetime.utcnow()
    cutoff = now - _TRANSIENT_ORPHAN_TTL
    query = {
        "folderId": folder["_id"],
        "meta.%s" % TRANSIENT_STAGED_META_KEY: True,
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


def _streamMultipartFileIntoItem(folder, user, part, name):
    """Stream one parsed multipart file part into a fresh item under ``folder``.

    ``folder`` is the WRITE-authorized document the ``stageInput`` route already
    loaded via its ``modelParam(level=AccessType.WRITE)`` decorator — the single
    authorization boundary, deliberately not re-checked here.
    """
    stream = getattr(part, "file", None)
    if stream is None:
        raise RestException("Staging request carries no file part", code=400)
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    if size <= 0:
        raise RestException("Staging file must not be empty", code=400)
    return Upload().uploadFromFile(
        stream,
        size=size,
        name=name,
        parentType="folder",
        parent=folder,
        user=user,
        mimeType="application/octet-stream",
    )


def _tagItemTransient(fileDoc):
    """Tag a freshly-uploaded file's parent item transient; return the item."""
    itemId = fileDoc.get("itemId")
    if not itemId:
        return None
    item = Item().load(itemId, force=True)
    if item:
        Item().setMetadata(item, {TRANSIENT_STAGED_META_KEY: True})
    return item
