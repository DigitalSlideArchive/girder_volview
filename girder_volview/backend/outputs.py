"""Processing backend -- folder-owned job-output correlation (write + cleanup).

Each backend job OWNS exactly one server-created private output folder. Every
output ``girder_worker`` uploads is forced into that folder, so an upload is
correlated back to its job by the finalized file's ACTUAL parent folder -- never
by a filename, a token, or a caller-supplied job id (all of which are
attacker-controllable). The read path (projecting recorded ids into result
intents) and status projection live in ``results.py``.

Ownership is also the deletion boundary, and it cascades both ways: a
``model.job.remove`` handler deletes the owned output folder plus any remaining
staged inputs and REFUSES a non-terminal owned job, while a
``model.folder.remove`` handler deletes the owning job when its output folder is
removed in the Girder hierarchy. Girder fires these handlers synchronously
BEFORE the DB delete and wraps them in no try/except, so raising aborts the
removal and retains the job record as the discoverable owner of whatever is left.
"""

import json

from girder.exceptions import RestException
from girder.models.folder import Folder
from girder.models.item import Item

# Module-object import (not ``from ... import Job``): call sites resolve
# ``girder_job.Job`` at call time, so tests may monkeypatch the class on
# ``girder_jobs.models.job`` and be seen here.
from girder_jobs.models import job as girder_job

from ..utils import JOB_OUTPUT_FOLDER_META_KEY, TRANSIENT_STAGED_META_KEY
from .inputs import _removeTransientItems

# Backend-owned job fields (otherFields, not a schema change). The id map is
# READ-exposed (``routes.addBackendRoutes``); the job's own ACL is the gate.
_OUTPUTS_FIELD = "volviewOutputs"  # {identifier: str(fileId)}
_OUTPUT_SPECS_FIELD = "volviewOutputSpecs"  # [{name, tag, isLabel, fileExtensions}]
_OUTPUT_FOLDER_ID_FIELD = "volviewOutputFolderId"  # str(folder _id) -- the job's
#   private output folder; the SOLE output-correlation and ownership key.


def _parseOutputReference(raw):
    """Decode an upload reference to a dict, or ``None``.

    ``girder_worker`` stamps each output upload with slicer_cli_web's JSON
    reference (``prepare_task.py`` -- carries the output ``identifier``). A
    non-JSON / non-dict / identifier-less reference (a foreign upload, or the
    backend's own reference-less staging upload) yields ``None`` so the handler
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


def _declaredOutputIdentifiers(job):
    """The set of output identifiers the job DECLARED at submit (or empty).

    An upload whose identifier is not one this job declared is never recorded --
    a crafted reference cannot introduce an undeclared output key.
    """
    specs = (job or {}).get(_OUTPUT_SPECS_FIELD)
    if not isinstance(specs, list):
        return set()
    return {
        spec["name"]
        for spec in specs
        if isinstance(spec, dict) and isinstance(spec.get("name"), str)
    }


def _jobForOutputFolder(folderId):
    """Find the job that OWNS a given output folder, or ``None`` (fail closed).

    Correlation is solely by the finalized file's actual parent folder: each job
    owns exactly one private output folder, so the parent folder uniquely
    identifies the job. A caller-supplied jobId / uuid / token / filename in the
    upload reference is NEVER consulted. The id is stored as a string on the job,
    so the query stringifies the parent folder id to match.
    """

    if folderId is None:
        return None
    job = girder_job.Job().findOne({_OUTPUT_FOLDER_ID_FIELD: str(folderId)})
    return job if isinstance(job, dict) else None


def _recordJobOutput(event):
    """Synchronously record a finalized output file's id onto its owning job.

    Correlate the upload to a job by the finalized file's ACTUAL parent folder (a
    File doc carries only ``itemId``, so this hops item -> folder), require the
    reference ``identifier`` to be one the job DECLARED, and record the file id
    keyed by that identifier (``otherFields`` dotted key -> Mongo ``$set`` nests
    per identifier, so N outputs each bind without overwriting). Fail closed: a
    foreign or uncorrelated upload, or an undeclared / unsafe identifier, is
    ignored. This event fires synchronously before ``finalizeUpload`` returns, so
    a DB failure while recording FAILS the upload (and the worker task) instead of
    allowing a false SUCCESS with an unbound result.
    """
    info = getattr(event, "info", None)
    if not isinstance(info, dict):
        return
    upload = info.get("upload")
    if not isinstance(upload, dict):
        return
    ref = _parseOutputReference(upload.get("reference"))
    if ref is None:
        return
    fileDoc = info.get("file")
    if not isinstance(fileDoc, dict):
        return
    fileId = fileDoc.get("_id")
    itemId = fileDoc.get("itemId")
    if not fileId or not itemId:
        return
    # A File doc has no ``folderId``; only its parent Item does. The item's parent
    # folder is the job-correlation key.
    item = Item().load(itemId, force=True, exc=False)
    parentFolderId = item.get("folderId") if isinstance(item, dict) else None
    if parentFolderId is None:
        return
    job = _jobForOutputFolder(parentFolderId)
    if not isinstance(job, dict):
        return
    identifier = ref["identifier"]
    if identifier not in _declaredOutputIdentifiers(job):
        # An upload into the job's own folder whose identifier the job never
        # declared is still refused -- correlation binds only declared outputs.
        return

    girder_job.Job().updateJob(
        job,
        otherFields={"%s.%s" % (_OUTPUTS_FIELD, identifier): str(fileId)},
    )


def _cascadeDeleteJobOwnedResources(event):
    """``model.job.remove`` handler: enforce job/output ownership on deletion.

    For a job that owns an output folder (``_OUTPUT_FOLDER_ID_FIELD`` present):

      * REFUSE to remove a non-terminal job -- raise so Girder never reaches the
        DB delete (this protects our own DELETE route AND Girder's built-in job
        route and any direct ``Job.remove`` caller);
      * otherwise cascade-delete the owned output folder (``Folder().remove``
        cascades to its items / subfolders / pending uploads) and then any
        remaining staged input items;
      * treat an already-missing owned folder as success (deletion is retryable);
      * let a folder-removal failure PROPAGATE so the model retains the job record
        -- the retained record stays the discoverable owner of whatever is left,
        and a later retry completes the cascade.

    Jobs without the ownership field are untouched (standard removal proceeds).
    """
    from .results import isTerminalStatus

    job = getattr(event, "info", None)
    if not isinstance(job, dict):
        return
    folderId = job.get(_OUTPUT_FOLDER_ID_FIELD)
    transientItemIds = job.get(TRANSIENT_STAGED_META_KEY) or []
    if not folderId and not transientItemIds:
        return  # not an owned job -- do not interfere with standard removal
    if folderId:
        if not isTerminalStatus(job.get("status")):
            raise RestException(
                "Cannot delete a job that is not finished", code=409
            )
        # The output folder is the critical owned resource (it holds the
        # results); a failure here propagates so the job is retained and the
        # delete is retryable. The in-progress marker (NOT a DB unset, which
        # would break retryability on a partial failure) stops the
        # model.folder.remove reverse cascade from re-entering Job.remove
        # for this same job mid-delete.
        folder = Folder().load(folderId, force=True, exc=False)
        if folder is not None:
            _CASCADING_FOLDER_IDS.add(str(folderId))
            try:
                Folder().remove(folder)
            finally:
                _CASCADING_FOLDER_IDS.discard(str(folderId))
    # Staged inputs are transient (also orphan-swept); clean them best-effort so a
    # stray input never blocks removing the (already-gone) results folder + job.
    # Swept even when the folder pointer is absent: the reverse cascade unsets it
    # before re-entering removal, and its jobs still own their staged inputs.
    _removeTransientItems(transientItemIds)


# Output-folder ids currently being removed by the job-side cascade above.
# GIL-atomic set mutations; entries live only for the synchronous span of one
# Folder().remove call, so the reverse handler below can tell "the job is
# deleting its own folder" apart from "a user deleted the folder in Girder".
_CASCADING_FOLDER_IDS = set()


def _cascadeDeleteFolderOwnedJob(event):
    """``model.folder.remove`` handler: removing a job's output folder removes the job.

    The reverse of ``_cascadeDeleteJobOwnedResources``: the private output
    folder is the job's sole owned storage, so deleting it in the Girder
    hierarchy means "delete this job". Removing the ``volview-jobs`` container
    recurses through the per-job subfolders, firing this handler once per job.

    Mirrored invariants:

    * REFUSE to remove a live job's folder -- a non-terminal owned job raises,
      aborting the folder removal (same guard as the job-side cascade);
    * recursion guard -- unset ``volviewOutputFolderId`` on the job (DB + the
      in-memory doc) BEFORE ``girder_job.Job().remove``, so the job-side cascade sees
      no owned folder and only sweeps staged inputs. Unsetting is safe in this
      direction: the folder is going away regardless, so a retained pointer
      could only dangle.

    No-ops fail closed: an unmarked folder, a folder no job owns (the container,
    a pre-publication orphan, an already-cascaded delete), or a removal driven
    by the job-side cascade itself (``_CASCADING_FOLDER_IDS``).
    """

    from .results import isTerminalStatus

    folder = getattr(event, "info", None)
    if not isinstance(folder, dict):
        return
    if not (folder.get("meta") or {}).get(JOB_OUTPUT_FOLDER_META_KEY):
        return
    folderId = folder.get("_id")
    if folderId is None or str(folderId) in _CASCADING_FOLDER_IDS:
        return
    job = _jobForOutputFolder(folderId)
    if job is None:
        return
    if not isTerminalStatus(job.get("status")):
        raise RestException(
            "Cannot delete the output folder of a job that is not finished; "
            "cancel the job first",
            code=409,
        )
    girder_job.Job().update(
        {"_id": job["_id"]}, {"$unset": {_OUTPUT_FOLDER_ID_FIELD: ""}}
    )
    job.pop(_OUTPUT_FOLDER_ID_FIELD, None)
    try:
        girder_job.Job().remove(job)
    except Exception:
        # Restore the ownership pointer so a failed job removal never orphans
        # the history row: the raise aborts the folder delete (the shell is
        # retained), and the restored pointer keeps the job correlated with it
        # for a later retry.
        girder_job.Job().update(
            {"_id": job["_id"]},
            {"$set": {_OUTPUT_FOLDER_ID_FIELD: str(folderId)}},
        )
        job[_OUTPUT_FOLDER_ID_FIELD] = str(folderId)
        raise


def _folderChainMatchesTargets(folderId, folderTargets, baseTargets):
    """Whether ``folderId`` is/descends from any folder in ``folderTargets``,
    or its ROOT folder hangs directly under any ``(parentType, id)`` pair in
    ``baseTargets`` (a collection or user about to be recursively deleted).

    Walks the folder's parent chain upward (owned output folders nest a couple
    of levels deep, so the chain is short). Fail closed on a missing folder;
    a (corrupt) parent cycle terminates via ``seen``.
    """
    seen = set()
    currentId = folderId
    while currentId is not None and str(currentId) not in seen:
        if str(currentId) in folderTargets:
            return True
        seen.add(str(currentId))
        folder = Folder().load(currentId, force=True, exc=False)
        if not isinstance(folder, dict):
            return False
        if folder.get("parentCollection") != "folder":
            return (
                folder.get("parentCollection"),
                str(folder.get("parentId")),
            ) in baseTargets
        currentId = folder.get("parentId")
    return False


def _liveJobOwningFolderUnderTargets(folderIds=(), baseParents=()):
    """The first non-terminal job whose owned output folder is one of
    ``folderIds``, a descendant of one, or contained (transitively) in any
    ``(parentType, id)`` collection/user in ``baseParents`` — else ``None``.

    Persisted job ownership is the AUTHORITATIVE record: neither the deleted
    folder nor anything on the path needs to carry the folder marker, so
    deleting an unmarked ancestor (the launch folder, a project root) or a
    folder whose marker was stripped cannot bypass the guard. Live owned jobs
    are few at any moment, so checking each one's ancestor chain is cheap; the
    query excludes settled history via the indexed ownership field + status.
    """

    from .results import terminalStatuses

    folderTargets = {str(folderId) for folderId in folderIds}
    baseTargets = {(parentType, str(_id)) for parentType, _id in baseParents}
    if not folderTargets and not baseTargets:
        return None
    jobs = girder_job.Job().find(
        {
            _OUTPUT_FOLDER_ID_FIELD: {"$exists": True},
            "status": {"$nin": list(terminalStatuses())},
        }
    )
    for job in jobs:
        if _folderChainMatchesTargets(
            job.get(_OUTPUT_FOLDER_ID_FIELD), folderTargets, baseTargets
        ):
            return job
    return None


def _refuseIfLiveJobUnder(folderIds=(), baseParents=()):
    if _liveJobOwningFolderUnderTargets(folderIds, baseParents) is not None:
        raise RestException(
            "Cannot delete a processing job's output folder while the job is "
            "still running; cancel the job first",
            code=409,
        )


def _refuseLiveJobFolderRestDelete(event):
    """``rest.delete.folder/:id.before`` guard: 409 a live job's folder delete.

    ``Folder.remove`` deletes contents (``clean``) BEFORE ``model.folder.remove``
    fires, so the reverse-cascade handler's non-terminal refusal can only save
    the folder shell, not what was inside. This REST-level guard runs before the
    delete handler touches anything, covering a live job's own folder, the
    ``volview-jobs`` container, and ANY ancestor of either (deleting the launch
    folder or a project root recursively cleans the job folder just the same).
    Direct model callers bypass REST and still hit the (shell-level) late guard.
    """
    info = getattr(event, "info", None)
    folderId = (info or {}).get("id")
    if folderId:
        _refuseIfLiveJobUnder(folderIds=[folderId])


def _refuseLiveJobCollectionRestDelete(event):
    """``rest.delete.collection/:id.before``: the same preflight for collection
    deletion, which recursively removes every folder inside without ever
    passing through ``DELETE /folder/:id``."""
    info = getattr(event, "info", None)
    collectionId = (info or {}).get("id")
    if collectionId:
        _refuseIfLiveJobUnder(baseParents=[("collection", collectionId)])


def _refuseLiveJobUserRestDelete(event):
    """``rest.delete.user/:id.before``: the same preflight for user deletion
    (removes the user's whole folder tree)."""
    info = getattr(event, "info", None)
    userId = (info or {}).get("id")
    if userId:
        _refuseIfLiveJobUnder(baseParents=[("user", userId)])


def _refuseLiveJobResourceRestDelete(event):
    """``rest.delete.resource.before``: the same preflight for the batch
    ``DELETE /resource`` route (arbitrary folder/collection/user ids).

    A malformed ``resources`` payload is left for the route itself to 400;
    item ids are ignored (an item cannot contain a job's output folder).
    """
    info = getattr(event, "info", None)
    raw = ((info or {}).get("params") or {}).get("resources")
    try:
        resources = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return
    if not isinstance(resources, dict):
        return
    ids = {
        model: [_id for _id in values if _id]
        for model, values in resources.items()
        if isinstance(values, list)
    }
    _refuseIfLiveJobUnder(
        folderIds=ids.get("folder", ()),
        baseParents=[
            (parentType, _id)
            for parentType in ("collection", "user")
            for _id in ids.get(parentType, ())
        ],
    )
