"""Processing backend -- REST routes + job creation + route registration.

Cross-module helper calls are MODULE-QUALIFIED on purpose (``submit._foo`` /
``inputs._foo`` / ``outputs._foo`` / ``results._foo``) so a test that patches a
helper on its DEFINING module reaches the call site here; a bare
``from .submit import _foo`` binds a name ``setattr(submit, "_foo", ...)``
cannot reach.
"""

import base64
import binascii
import copy
import datetime
import json
import threading
import time
import uuid

import cherrypy
from girder import events, logger
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource, boundHandler
from girder.constants import AccessType, SortDir, TokenScope
from girder.exceptions import RestException, ValidationException
from girder.models.folder import Folder

# Module-object import (not ``from ... import Job``): call sites resolve
# ``girder_job.Job`` at call time, so tests may monkeypatch the class on
# ``girder_jobs.models.job`` and be seen here.
from girder_jobs.models import job as girder_job

from ..utils import (
    _toIso,
    makeFileDownloadUrl,
    isJobOutputFolderMarked,
    JOB_OUTPUT_FOLDER_META_KEY,
    TRANSIENT_STAGED_META_KEY,
)
from .config import PROCESSING_ROUTE_NAME
from .slicer_spec import declared_params, translate_slicer_xml, validate_task_spec
from . import inputs, submit, outputs, results


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
    if user and submit._slicerCliAvailable():
        try:
            tasks.extend(
                [submit._cliItemToSummary(c) for c in submit._scopedCliItems(user)]
            )
        except Exception:
            logger.exception("Failed to list slicer_cli_web items")
    return tasks


# Explicit short lifetime for the CLI-container token. Without ``days`` Girder
# applies ``core.cookie_lifetime`` (180 days by default), leaving a broad
# data-plane credential valid for months after the job ends. One day covers queue
# wait + run time and bounds exposure if the token leaks (a compromised image,
# broker, or command-line capture).
_CONTAINER_TOKEN_TTL_DAYS = 1.0

JOB_HISTORY_PAGE_DEFAULT = 25
JOB_HISTORY_PAGE_MAX = 100
JOB_HISTORY_INDEX = "volview_job_history"
JOB_OUTPUT_FOLDER_INDEX = "volview_output_folder"
_SUBMISSION_ID_FIELD = "volviewSubmissionId"
_SUBMITTED_PARAMETERS_FIELD = "volviewSubmittedParameters"


def ensureJobHistoryIndexes(jobModel=None):
    """Install the indexes the history list and output-correlation queries need.

    A compound index backs the personal newest-first history page. A point-lookup
    index on the private output-folder id backs ``outputs._jobForOutputFolder``,
    which runs a ``findOne`` for EVERY finalized output upload -- without it each
    correlation is a full jobs-collection scan that worsens as history grows.
    """
    if jobModel is None:
        jobModel = girder_job.Job()
    jobModel.collection.create_index(
        [
            (inputs._LAUNCH_FOLDER_FIELD, 1),
            ("userId", 1),
            ("created", -1),
            ("_id", -1),
        ],
        name=JOB_HISTORY_INDEX,
    )
    jobModel.collection.create_index(
        [(outputs._OUTPUT_FOLDER_ID_FIELD, 1)],
        name=JOB_OUTPUT_FOLDER_INDEX,
    )


def _encodeJobCursor(job):
    payload = json.dumps(
        {
            "created": _toIso(job.get("created")),
            "id": str(job["_id"]),
        },
        separators=(",", ":"),
    ).encode("utf8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decodeJobCursor(cursor):
    from bson.objectid import ObjectId

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf8"))
        created = datetime.datetime.fromisoformat(value["created"])
        if created.tzinfo is not None:
            created = created.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return created, ObjectId(value["id"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, binascii.Error):
        raise RestException("Invalid job history cursor", code=400) from None


def _jobHistoryPageSize(limit):
    try:
        pageSize = int(limit if limit is not None else JOB_HISTORY_PAGE_DEFAULT)
        if pageSize < 1 or pageSize > JOB_HISTORY_PAGE_MAX:
            raise ValueError()
        return pageSize
    except (TypeError, ValueError):
        raise RestException("Invalid job history limit", code=400) from None


def _jobCursorContinuation(cursor):
    created, jobId = _decodeJobCursor(cursor)
    return [
        {"created": {"$lt": created}},
        {"created": created, "_id": {"$lt": jobId}},
    ]


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("List the current user's complete processing-job history.")
    .notes(
        "Returns a bounded newest-first page of lightweight summaries. The "
        "continuation cursor is opaque; pagination bounds responses, never "
        "history retention. Logs and submitted parameters are detail-only."
    )
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("limit", "Page size (1-100).", required=False, dataType="integer")
    .param("cursor", "Opaque continuation cursor.", required=False)
    .produces(["application/json"])
)
def listJobHistory(self, folder, limit=JOB_HISTORY_PAGE_DEFAULT, cursor=None):
    user = self.getCurrentUser()
    if not user:
        return {"jobs": [], "nextCursor": None}

    pageSize = _jobHistoryPageSize(limit)
    query = {
        inputs._LAUNCH_FOLDER_FIELD: str(folder["_id"]),
        "userId": user["_id"],
    }
    if cursor:
        query["$or"] = _jobCursorContinuation(cursor)
    found = girder_job.Job().findWithPermissions(
        query=query,
        user=user,
        jobUser=user,
        level=AccessType.READ,
        sort=[("created", SortDir.DESCENDING), ("_id", SortDir.DESCENDING)],
        limit=pageSize + 1,
        # The summary projection never reads the log, which is unbounded (multi-MB
        # on chatty/failed CLIs), so exclude it from every page. Mirrors
        # Job.load(includeLog=False)'s {'log': False} projection.
        fields={"log": False},
    )
    page = list(found)
    hasMore = len(page) > pageSize
    page = page[:pageSize]
    readableOutputFiles = results._readableOutputFilesForJobs(page, user)
    return {
        "jobs": [
            results._projectJobHistorySummary(
                job, user, readableOutputFiles=readableOutputFiles
            )
            for job in page
        ],
        "nextCursor": _encodeJobCursor(page[-1]) if hasMore else None,
    }


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get the VolView task spec for a task.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("taskId", "The task identifier.", paramType="path")
)
def getTaskSpec(self, folder, taskId):
    # The Slicer XML is translated into VolView's task spec server-side, so the
    # client never parses backend XML.
    user = self.getCurrentUser()
    if not submit._slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=404)
    scoped = submit._findScopedCliItem(taskId, user)
    if not scoped:
        raise RestException("Unknown taskId", code=404)
    # translate_slicer_xml needs the strict <executable> parse (title/description
    # + ordered params), which ``parse_cli`` does not carry, so it parses the XML
    # itself; the scoped parse is consumed by runTask.
    cliItem, _parsedCli = scoped
    try:
        return validate_task_spec(translate_slicer_xml(cliItem.xml, str(taskId)))
    except ValueError as exc:
        logger.error("Invalid VolView task spec for task %s: %s", taskId, exc)
        raise RestException("Task specification is invalid", code=500) from None


# The single server-owned container every per-job output folder nests inside: one
# hierarchy entry per launch folder no matter how many jobs accumulate, and one
# ADMIN-gated "clear this dataset's job history" gesture (removing it recurses
# through the per-job folders, firing the reverse cascade per job). The name is
# reserved: a pre-existing USER folder with this name is never adopted -- reuse is
# gated on the server-owned marker, and an unmarked name collision refuses the
# submission (409) instead.
JOBS_CONTAINER_NAME = "volview-jobs"

# The launch folder's own authoritative pointer to its container, stamped by
# whichever concurrent caller wins the atomic election below. A plain name
# lookup cannot serve this role: Girder's folder-uniqueness check is
# application-level (``Folder.validate`` does a check-then-act ``findOne``,
# and the backing index -- see ``Folder.initialize`` -- is not declared
# unique), so concurrent ``createFolder`` calls for the SAME name can all
# succeed with no ``ValidationException`` at all. Electing a winner by racing
# a single atomic ``find_one_and_update`` against one document (the launch
# folder) is what actually serializes concurrent callers; name uniqueness
# cannot.
_JOBS_CONTAINER_ID_META_KEY = "volviewJobsContainerId"

# Bounds the election loop below. Every iteration either returns or makes
# irreversible progress (heals a stale pointer, or loses a creation race and
# deletes its own orphaned attempt), so real contention resolves in at most
# two or three passes; this is a defensive ceiling against a pathological
# repeated-contention run, not an expected trip count.
_JOBS_CONTAINER_ELECTION_ATTEMPTS = 10

# Creating a container is 3 separate writes (create, ACL replace, marker
# stamp), so a colliding concurrent creator can observe it mid-flight --
# named, but not yet marked. Dedup is the atomic election above, which never
# waits; this bounded grace period answers only the narrower "is the collision
# one of ours, still finishing its own stamp" question, which reads wrong if
# asked one write too early. Polled finely so the common case exits in tens of
# milliseconds, over a total budget wide enough that a loaded server still
# finishing those writes is never mistaken for a user's folder.
_COLLISION_MARK_GRACE_ATTEMPTS = 50
_COLLISION_MARK_GRACE_INTERVAL = 0.02

# How many election passes may end in an unmarked same-name collision before it
# is reported as a user's folder. A grace period is a timing guess, while the
# election's own fast path is where a slow race-mate's container actually lands,
# so one expiry is not a verdict -- only a collision that survives a re-run is.
_UNMARKED_COLLISION_ATTEMPTS = 2


def _validRecordedContainer(containerId, launchFolder):
    """The recorded container if it is still a live, correctly-parented,
    marked jobs container for ``launchFolder``; ``None`` if the record is
    stale (folder removed, reparented, or its marker stripped)."""
    if not containerId:
        return None
    try:
        candidate = Folder().load(containerId, force=True, exc=False)
    except Exception:
        return None
    if not isinstance(candidate, dict):
        return None
    if str(candidate.get("parentId")) != str(launchFolder["_id"]):
        return None
    if candidate.get("parentCollection") != "folder":
        return None
    if not isJobOutputFolderMarked(candidate):
        return None
    return candidate


def _claimJobsContainerId(launchFolder, containerId):
    """Atomically record ``containerId`` as the launch folder's container,
    ONLY if nothing is recorded yet. Returns whether this call won."""
    return (
        Folder().collection.find_one_and_update(
            {
                "_id": launchFolder["_id"],
                "meta.%s" % _JOBS_CONTAINER_ID_META_KEY: {"$exists": False},
            },
            {"$set": {"meta.%s" % _JOBS_CONTAINER_ID_META_KEY: str(containerId)}},
        )
        is not None
    )


def _awaitCollisionMarked(launchFolder):
    """The reserved-name folder colliding with a lost ``createFolder`` call,
    once (and if) it is MARKED, within a short grace period. Returns the
    last-observed folder doc if it is still unmarked once the grace period
    elapses; ``None`` only if it vanished (removed by someone). Never itself
    concludes 409 -- see the caller."""
    colliding = None
    for _ in range(_COLLISION_MARK_GRACE_ATTEMPTS):
        colliding = Folder().findOne(
            {
                "parentId": launchFolder["_id"],
                "parentCollection": "folder",
                "name": JOBS_CONTAINER_NAME,
            }
        )
        if colliding is None or isJobOutputFolderMarked(colliding):
            return colliding
        time.sleep(_COLLISION_MARK_GRACE_INTERVAL)
    return colliding


def _launchFolderRemovedError():
    """Every later election step needs a live parent to create or record
    against, so losing one mid-flight is a 409 wherever it is noticed."""
    return RestException(
        "The launch folder was removed while its processing jobs "
        "container was being established",
        code=409,
    )


def _reloadLaunchFolder(launchFolderId):
    """The launch folder's current doc, or a 409 if it was removed."""
    folder = Folder().load(launchFolderId, force=True)
    if folder is None:
        raise _launchFolderRemovedError()
    return folder


def _jobsContainerFolder(launchFolder, user):
    """Create-or-reuse the launch folder's server-owned ``volview-jobs`` container.

    Optimistically create, atomically record, losers self-delete:

    1. Fast path: if the launch folder already records a container id (a meta
       key on the LAUNCH folder, not the container), load and validate it
       (exists, correctly parented, marked). If valid, adopt it -- no write.
    2. Otherwise create a candidate container (``createFolder`` + ACL replace
       + the ``volviewJobOutputFolder`` marker other code reads), then race a
       single atomic ``find_one_and_update`` to record it on the launch
       folder, guarded on nothing being recorded yet.
    3. Losing that race normally means a concurrent caller's candidate was
       recorded first: this call's own candidate was never returned to anyone,
       so it is deleted, and the (now-recorded) winner is adopted. The one
       exception is a win by proxy: an adopter (step 2's collision path) can
       find THIS call's candidate already marked and record it before this
       call's own claim runs. A failed claim therefore only licenses deletion
       after re-reading the pointer and confirming the recorded winner is a
       DIFFERENT folder -- a recorded winner that IS this candidate is
       returned, never deleted (the adopter already returned it to its
       caller).
    4. A stale record -- the container was removed or reparented out from
       under it after being recorded -- is healed by clearing the pointer,
       guarded on the exact stale value so a concurrent healer/re-recorder is
       never clobbered, and the election re-runs.

    Reuse requires the ``volviewJobOutputFolder`` marker; the reserved name
    alone is not enough:

    * Why a marker: adopting a user's pre-existing folder that merely shares
      the reserved name would silently hide its contents from launch
      manifests and turn the container-delete gesture into "delete unrelated
      user data". An unmarked name collision refuses the submission with a
      409 instead of being adopted.
    * A same-named collision at ``createFolder`` time (``ValidationException``)
      is therefore never itself a race signal (concurrent callers routinely
      clear ``createFolder`` with no exception at all -- see the module note
      above); it means SOMETHING not already known to this call already holds
      the name. If that something is marked, it is either a race-mate that
      already won the election or an orphan left by a process that created
      and marked a container but crashed before recording it -- either way it
      is adopted by (trying to) record it. If it is unmarked, a short grace
      period (:data:`_COLLISION_MARK_GRACE_ATTEMPTS`) gives a race-mate whose
      ``createFolder`` landed first, but whose ACL replace/marker stamp
      hasn't yet, a chance to finish becoming marked; still unmarked after
      that is refused (409), never adopted. This is the one remaining wait in
      the function -- not a substitute for the atomic election, which never
      waits on anything, but a concession to container creation itself being
      3 separate writes.
    * ACL: after creation, the container's ACL is replaced with the launch
      folder's exact user and group policy. This removes the implicit ADMIN
      grant ``createFolder`` gives its creator while retaining collaborators'
      inherited access. Each per-job folder inside keeps its own
      submitter-only ACL.
    * Failed stamp: if the ACL replace or marker stamp raises, the
      just-created folder is removed rather than left behind unmarked (which
      would permanently 409 every future submission via the collision path
      above -- nothing else knows to adopt an unmarked folder).
    """
    launchFolderId = launchFolder["_id"]
    unmarkedCollisions = 0
    for _ in range(_JOBS_CONTAINER_ELECTION_ATTEMPTS):
        recordedId = (launchFolder.get("meta") or {}).get(_JOBS_CONTAINER_ID_META_KEY)
        if recordedId:
            container = _validRecordedContainer(recordedId, launchFolder)
            if container is not None:
                return container
            Folder().collection.find_one_and_update(
                {
                    "_id": launchFolderId,
                    "meta.%s" % _JOBS_CONTAINER_ID_META_KEY: recordedId,
                },
                {"$unset": {"meta.%s" % _JOBS_CONTAINER_ID_META_KEY: ""}},
            )
            launchFolder = _reloadLaunchFolder(launchFolderId)
            continue

        try:
            created = Folder().createFolder(
                parent=launchFolder,
                name=JOBS_CONTAINER_NAME,
                parentType="folder",
                creator=user,
                public=False,
            )
        except ValidationException:
            colliding = _awaitCollisionMarked(launchFolder)
            if colliding is None:
                # The colliding folder vanished (removed by someone else) --
                # try the whole election again.
                launchFolder = _reloadLaunchFolder(launchFolderId)
                continue
            if not isJobOutputFolderMarked(colliding):
                unmarkedCollisions += 1
                if unmarkedCollisions < _UNMARKED_COLLISION_ATTEMPTS:
                    # Re-run instead: the next pass reads the recorded pointer
                    # first, which is where a race-mate slower than the grace
                    # period lands its container.
                    launchFolder = _reloadLaunchFolder(launchFolderId)
                    continue
                raise RestException(
                    "A folder named '%s' already exists here and is not a "
                    "processing jobs container; rename or remove it to run "
                    "processing tasks" % JOBS_CONTAINER_NAME,
                    code=409,
                ) from None
            if _claimJobsContainerId(launchFolder, colliding["_id"]):
                return colliding
            launchFolder = _reloadLaunchFolder(launchFolderId)
            continue

        try:
            created = Folder().setAccessList(
                created,
                launchFolder.get("access", {"users": [], "groups": []}),
                save=True,
                force=True,
            )
            created = Folder().setMetadata(created, {JOB_OUTPUT_FOLDER_META_KEY: True})
        except Exception:
            Folder().remove(created)
            raise

        if _claimJobsContainerId(launchFolder, created["_id"]):
            return created
        # A failed claim does not mean this candidate lost: an adopter that
        # lost ``createFolder`` to it can find it already marked and record it
        # before this claim runs (a win by proxy), and by then the adopter has
        # returned it to its own caller. Only a recorded winner that is a
        # DIFFERENT folder makes this candidate unreferenced and safe to
        # delete.
        launchFolder = Folder().load(launchFolderId, force=True)
        recordedId = ((launchFolder or {}).get("meta") or {}).get(
            _JOBS_CONTAINER_ID_META_KEY
        )
        if recordedId == str(created["_id"]):
            return created
        Folder().remove(created)
        if launchFolder is None:
            raise _launchFolderRemovedError()

    raise RestException(
        "Could not establish this launch folder's processing jobs container "
        "after repeated contention; try again",
        code=500,
    )


def _createJobOutputFolder(launchFolder, user, submissionId):
    """Create the job's private, server-owned output folder for a submission.

    Lives inside the launch folder's ``volview-jobs`` container. Every
    declared output is forced into this folder, and it is the SOLE
    output-correlation + ownership key. Two steps make it private:

    * mark it ``volviewJobOutputFolder`` so the launch manifest excludes it and
      its contents (job results take the job path only, never ordinary launch
      data);
    * REPLACE its ACL with a submitter-only ADMIN list. ``createFolder`` copies
      the parent's ACL (``copyAccessPolicies``), which would otherwise leave
      every launch-folder collaborator able to read the private results;
      ``setAccessList(..., force=True, setPublic=False)`` strips that. Girder
      system administrators keep their normal force access.
    """
    created = Folder().createFolder(
        parent=_jobsContainerFolder(launchFolder, user),
        name="volview-job-%s" % submissionId,
        parentType="folder",
        creator=user,
        public=False,
        reuseExisting=False,
    )
    try:
        folder = Folder().setMetadata(created, {JOB_OUTPUT_FOLDER_META_KEY: True})
        Folder().setAccessList(
            folder,
            {
                "users": [{"id": user["_id"], "level": AccessType.ADMIN}],
                "groups": [],
            },
            save=True,
            force=True,
            setPublic=False,
        )
        return folder
    except Exception:
        try:
            Folder().remove(created)
        except Exception:
            logger.exception(
                "Failed to remove partially initialized job output folder %s",
                created.get("_id"),
            )
        raise


def _removeJobOutputFolder(folder):
    """Best-effort removal of a pre-publication output folder (no job yet).

    Only called when a submission failed BEFORE any job was created, so no
    ownership record exists to drive the normal deletion cascade. An
    already-missing folder is a no-op.
    """
    if not folder:
        return
    try:
        Folder().remove(folder)
    except Exception:
        logger.exception(
            "Failed to remove orphaned job output folder %s", folder.get("_id")
        )


def _requestCliItem(cliItem, initialJobFields):
    """Copy a catalog CLI item and inject request-local initial job fields."""
    requestItem = copy.copy(cliItem)
    requestItem.item = copy.deepcopy(getattr(cliItem, "item", {}) or {})
    meta = requestItem.item.setdefault("meta", {})
    dockerParams = meta.setdefault("docker-params", {})
    if not isinstance(dockerParams, dict):
        raise ValidationException("CLI docker parameters are malformed")
    catalogFields = dockerParams.get("girder_job_other_fields") or {}
    if not isinstance(catalogFields, dict):
        raise ValidationException("CLI initial job fields are malformed")
    merged = dict(catalogFields)
    merged.update(initialJobFields)
    dockerParams["girder_job_other_fields"] = merged
    return requestItem


def _genDockerJob(cliItem, params, user, initialJobFields):
    """Create the slicer_cli_web docker job for a CLI item and return its doc.

    Isolated as the single live slicer_cli_web touch point so ``runTask`` (and
    its tests) can drive job creation without the optional dependency.
    """
    from girder.models.token import Token
    from slicer_cli_web.rest_slicer_cli import genHandlerToRunDockerCLI

    # Scope-limit the container token to the data plane the CLI actually needs:
    # read its inputs, write its outputs. Narrower than the ecosystem norm
    # (slicer_cli_web mints full-auth tokens) without weakening Girder ACLs — the
    # submitter's own read/write reach still bounds it. It is NOT persisted on the
    # job or used as an ownership/correlation key (outputs bind by their private
    # parent folder, never by a token).
    token = Token().createToken(
        user=user,
        scope=[TokenScope.DATA_READ, TokenScope.DATA_WRITE],
        days=_CONTAINER_TOKEN_TTL_DAYS,
    )
    requestItem = _requestCliItem(cliItem, initialJobFields)
    handler = genHandlerToRunDockerCLI(requestItem)
    # slicer_cli_web only substitutes its GirderApiUrl()/GirderToken() runtime
    # transforms when these keys are present in the params it processes
    # (prepare_task `_add_optional_input_param` skips a param absent from args).
    # The REST route defaults them in, but this backend calls `subHandler`
    # directly. Empty -> the transforms substitute, and GirderToken resolves to
    # THIS scoped token, not a broader one. Without them a CLI that fetches its own
    # inputs by id (`reference="_girder_id_"`, e.g. a multi-file DICOM series) has
    # no way to reach Girder. Harmless for a CLI that declares neither --
    # slicer_cli_web ignores undeclared args.
    params = dict(params)
    params.setdefault("girderApiUrl", "")
    params.setdefault("girderToken", "")
    # Take a copy so the handler can mutate freely.
    job_obj = handler.subHandler(requestItem, copy.deepcopy(params), user, token)
    job = job_obj.job if hasattr(job_obj, "job") else job_obj
    return job


def _prepareSubmissionFields(
    submissionId, folder, taskId, values, outputSpecs, transientItemIds, outputFolder
):
    """Build every job association field before task publication.

    Includes the job's private output-folder id (``_OUTPUT_FOLDER_ID_FIELD``) —
    the sole output-correlation + ownership key — so the folder id is part of the
    FIRST job insert, queryable before any worker upload can race in.

    ``outputSpecs`` is the ``slicer_spec.parse_cli`` output descriptor list
    ``runTask`` already parsed, threaded in rather than re-parsed here.
    """
    fields = {
        _SUBMISSION_ID_FIELD: submissionId,
        inputs._LAUNCH_FOLDER_FIELD: str(folder["_id"]),
        inputs._TASK_ID_FIELD: str(taskId),
        outputs._OUTPUT_SPECS_FIELD: outputSpecs,
        outputs._OUTPUT_FOLDER_ID_FIELD: str(outputFolder["_id"]),
        outputs._OUTPUTS_FIELD: {},
        _SUBMITTED_PARAMETERS_FIELD: copy.deepcopy(values),
    }
    if transientItemIds:
        fields[TRANSIENT_STAGED_META_KEY] = list(transientItemIds)
    return fields


def _jobForSubmission(submissionId):

    return girder_job.Job().findOne({_SUBMISSION_ID_FIELD: submissionId})


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
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
    if not isinstance(values, dict):
        raise RestException("values must be an object of parameter values", code=400)

    # Reject a payload carrying reserved credentials before any task lookup or
    # work; declaration-aware screens run after the CLI XML is parsed below.
    submit._rejectReservedSubmitParams(values)

    if not submit._slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=500)

    scoped = submit._findScopedCliItem(taskId, user)
    if not scoped:
        raise RestException("Unknown taskId", code=404)
    cliItem, parsedCli = scoped

    # The submission parses the CLI XML exactly twice: ``_findScopedCliItem``
    # already ran ``parse_cli``, whose ``outputs`` are reused here, and
    # ``declared_params`` supplies the label-independent key/value declaration the
    # grouped walk can't. Downstream guard/translate steps read these structures.
    declared = declared_params(cliItem.xml)
    outputSpecs = parsedCli["outputs"]

    # Screens the RAW client keys, so it must run before autofill adds
    # server-owned output structures. A synthesized-folder collision, an
    # undeclared key, an out-of-declaration value, or a missing required input
    # is a boundary 400 naming the parameter, not a later job failure.
    submit._rejectSynthesizedFolderParams(values, declared)
    submit._rejectUndeclaredSubmitParams(values, declared)
    submit._validateDeclaredSubmitValues(values, declared)
    submit._rejectMissingRequiredParams(values, declared)

    # Auto-generate a deterministic output filename for any output param the user
    # didn't fill (input file + CLI name + parameter name + extension). Names need
    # not be unique: outputs bind to the job by its private output folder, not by
    # name, so a duplicate filename can never cross results.
    values = submit._autofillOutputs(dict(values), outputSpecs, cliItem.name)

    # The output folder is created BEFORE translating params or publishing the
    # task: every declared output is forced into it, and its id is part of the
    # first job insert so it is queryable before any worker upload can race in.
    submissionId = uuid.uuid4().hex
    outputFolder = _createJobOutputFolder(folder, user, submissionId)

    transientItemIds = []
    try:
        # Resolves each bound input once (own-scheme validation + per-user ACL
        # re-check), forces every declared output into the private output folder,
        # and rejects a client-supplied folderRef. The authorized file documents
        # are reused for transient detection so each URI's ACL check runs once.
        params, resolvedInputFiles = submit._translateValuesToSlicerParams(
            values, user, outputFolder, declared
        )
        # Per-job input ownership: any staged (transient) input is COPIED into the
        # job's private folder and the CLI params are rewritten onto the copies.
        # The copies are recorded on the job so
        # inputs._cleanupTransientOnJobDone deletes them at terminal state; the
        # shared staged original is never a job dependency.
        params, transientItemIds = inputs.copyStagedInputsIntoJobFolder(
            params, resolvedInputFiles, user, outputFolder
        )
        # INFO carries only routing identity; the translated CLI params can hold
        # sensitive string values, so they stay at debug.
        logger.info(
            "[volview_processing] runTask folder=%s task=%s submission=%s",
            folder["_id"],
            taskId,
            submissionId,
        )
        logger.debug("[volview_processing] runTask params=%s", params)

        initialFields = _prepareSubmissionFields(
            submissionId,
            folder,
            taskId,
            values,
            outputSpecs,
            transientItemIds,
            outputFolder,
        )
        job_doc = _genDockerJob(cliItem, params, user, initialFields)
    except Exception:
        # run.delay can fail after Girder Worker's before_task_publish handler
        # inserted the job. Resolve that ambiguity by the server-minted id.
        job_doc = _jobForSubmission(submissionId)
        if job_doc is None:
            # No job exists (including a folderRef-rejection 400 before any work):
            # remove the pre-publication output folder and staged inputs.
            _removeJobOutputFolder(outputFolder)
            inputs._removeTransientItems(transientItemIds)
        else:
            # A job WAS created: cancel it but RETAIN its ownership record so the
            # normal terminal + deletion cascade cleans the output folder safely.
            try:

                girder_job.Job().cancelJob(job_doc)
            except Exception:
                logger.exception(
                    "Failed to cancel ambiguously published job %s",
                    job_doc.get("_id"),
                )
        raise
    return {"jobId": str(job_doc["_id"])}


# Job-addressed routes are keyed by job id alone and gated by the job's OWN ACL.
# The launch folder is not part of a job's identity, so these carry no
# ``folderId``; they live on the folder-free ``volview_processing`` resource
# below. getJob / getJobResults are READ-gated; cancel and delete are WRITE-gated
# so a read-only viewer who can see a job's status cannot cancel or delete it.


def _loadJobForStatusProjection(jobId, user):
    """Load a job for status projection, WITHOUT its log unless it is needed.

    The client polls status every ~2s per live job and the job log grows
    unbounded, so the common load excludes it at the Mongo projection level
    (``includeLog`` defaults False). Only the terminal-error projection reads it
    (a bounded tail in ``results._projectJobStatus``), and the poller stops at
    terminal, so the log is reloaded at most once per job. The detail route is
    the full-log path.
    """

    job = girder_job.Job().load(jobId, user=user, level=AccessType.READ, exc=True)
    if results._projectJobState(job) == "error":
        job = girder_job.Job().load(
            jobId,
            user=user,
            level=AccessType.READ,
            exc=True,
            includeLog=True,
        )
    return job


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get job status.")
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJob(self, jobId):
    user = self.getCurrentUser()
    job = _loadJobForStatusProjection(jobId, user)
    return results._projectJobStatus(job, user)


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get detail-only job logs and submitted parameters.")
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJobHistoryDetail(self, jobId):
    user = self.getCurrentUser()

    job = girder_job.Job().load(
        jobId,
        user=user,
        level=AccessType.READ,
        exc=True,
        includeLog=True,
    )
    log = job.get("log") or []
    if not isinstance(log, list):
        log = [str(log)]
    parameters = job.get(_SUBMITTED_PARAMETERS_FIELD) or {}
    if not isinstance(parameters, dict):
        parameters = {}
    return {
        "jobId": str(job["_id"]),
        "log": [str(line) for line in log],
        "parameters": parameters,
    }


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description(
        "Delete a terminal job and its owned output folder + staged inputs."
    )
    .notes(
        "A pending or running job returns 409 (cancel it first). A terminal job "
        "is removed together with its private output folder and any remaining "
        "staged inputs — deleting the job also deletes its results."
    )
    .param("jobId", "The job identifier.", paramType="path")
    .errorResponse("Write access was denied for the job.", 403)
    .errorResponse("The job is still running.", 409)
)
def deleteJob(self, jobId):
    user = self.getCurrentUser()

    model = girder_job.Job()
    job = model.load(jobId, user=user, level=AccessType.WRITE, exc=True)
    # The model.job.remove handler ALSO enforces this (protecting other
    # Job.remove callers); the route returns the typed product response
    # rather than the model's raise.
    if not results.isTerminalStatus(job.get("status")):
        raise RestException(
            "Job is still running; cancel it before deleting", code=409
        )
    # The ownership cascade (owned output folder + staged inputs) lives in the
    # model.job.remove handler; this route must not add a second cascade.
    model.remove(job)
    cherrypy.response.status = 204
    return None


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get job results.")
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJobResults(self, jobId):
    user = self.getCurrentUser()

    job = girder_job.Job().load(jobId, user=user, level=AccessType.READ, exc=True)
    payload = results._jobResultsPayload(job, user)
    if payload.get("code"):
        cherrypy.response.status = 409
        if payload["resultState"] == "waiting":
            cherrypy.response.headers["Retry-After"] = "2"
    return payload


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description("Cancel a job.")
    .notes(
        "Best-effort: Girder only transitions an INACTIVE/QUEUED/RUNNING job to "
        "CANCELED, so cancelling an already-terminal job is a no-op. The response "
        "is the job's real projected status after the attempt -- never a "
        "fabricated 'cancelled' -- and the client's poller converges on whatever "
        "terminal state Girder ultimately reports."
    )
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
    .errorResponse("Write access was denied for the job.", 403)
)
def cancelJob(self, jobId):
    user = self.getCurrentUser()

    jobModel = girder_job.Job()
    job = jobModel.load(jobId, user=user, level=AccessType.WRITE, exc=True)
    try:
        jobModel.cancelJob(job)
    except ValidationException:
        # Girder refuses a CANCELED transition from a terminal state, so an
        # already-finished job cannot be cancelled. Fall through and report the
        # job's real state rather than fabricate a `cancelled` the poller would
        # contradict.
        pass
    fresh = _loadJobForStatusProjection(jobId, user)
    return results._projectJobStatus(fresh, user)


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description(
        "Stage a parent-bound labelmap or annotations file as a transient "
        "processing input."
    )
    .notes(
        "Accepts multipart resource bytes plus a neutral reference-image InputValue. "
        "The backend validates and resolves that opaque relationship against durable "
        "reference files before minting the staged URI. The created item is tagged "
        "transient, deleted when its job reaches a terminal state, or swept if "
        "never submitted."
    )
    .modelParam("folderId", model=Folder, level=AccessType.WRITE)
    .param(
        "file",
        "The staged resource bytes (labelmap or annotations JSON).",
        paramType="formData",
        dataType="file",
    )
    .jsonParam(
        "descriptor",
        "Typed staged resource descriptor (labelmap or annotations).",
        paramType="formData",
        requireObject=True,
    )
    .errorResponse()
)
def stageInput(self, folder, file, descriptor):
    user = self.getCurrentUser()
    # Validate the whole descriptor (reference image included) before writing
    # bytes so a malformed, foreign, unauthorized, or transient reference never
    # leaves an orphan upload.
    name = inputs.validateStagedDescriptor(descriptor, user)
    # Keep browser-generated working data out of the launch folder. The shared,
    # server-owned jobs container is already excluded from VolView's launch
    # manifest and carries the launch folder's collaborator ACL. Submission will
    # still copy this original into its private per-job folder before publishing
    # the task.
    stagingFolder = _jobsContainerFolder(folder, user)
    # Job-end cleanup never sees an upload that was never submitted, so age out
    # this jobs container's staged orphans before adding another.
    inputs._sweepOrphanTransients(stagingFolder)
    fileDoc = inputs._streamMultipartFileIntoItem(stagingFolder, user, file, name)
    try:
        inputs._tagItemTransient(fileDoc)
    except Exception:
        # An untagged item is invisible to both the TTL sweep and the
        # launch-manifest exclusion (each keys on the transient marker), so it
        # would linger as apparent durable launch data.
        inputs._removeTransientItems([fileDoc["itemId"]])
        raise
    # The backend mints the staged URI; the client constructs none.
    return {"uris": [makeFileDownloadUrl(fileDoc)]}


class _JobResource(Resource):
    """Folder-free REST surface for the job-addressed routes.

    Mounted at ``/volview_processing`` (a sibling of ``/folder``, ``/item``),
    this hosts status / results / cancel keyed by job id alone -- the launch
    folder is not part of a job's identity. The launch-context routes
    (tasks / spec / run / stage) stay on the folder tree because they operate
    per-folder. The handlers are the same module-level ``@boundHandler``
    functions; only their mount point differs.
    """

    def __init__(self):
        super().__init__()
        self.resourceName = PROCESSING_ROUTE_NAME
        self.route("GET", ("jobs", ":jobId"), getJob)
        self.route("GET", ("jobs", ":jobId", "detail"), getJobHistoryDetail)
        self.route("DELETE", ("jobs", ":jobId"), deleteJob)
        self.route("GET", ("jobs", ":jobId", "results"), getJobResults)
        self.route("POST", ("jobs", ":jobId", "cancel"), cancelJob)


def _ensureJobHistoryIndexesInBackground():
    """Kick the index builds off a daemon thread so plugin load never blocks.

    ``create_index`` is idempotent and a no-op on steady state, but the FIRST
    boot against a large pre-existing jobs collection waits for the whole build;
    the queries the indexes back merely degrade to scans until the build lands.
    """

    def build():
        try:
            ensureJobHistoryIndexes()
        except Exception:
            logger.exception("Failed to ensure volview job-history indexes")

    threading.Thread(
        target=build, name="volview-job-history-indexes", daemon=True
    ).start()


def addBackendRoutes(info):
    _ensureJobHistoryIndexesInBackground()
    # Delete a job's transient staged inputs once it reaches a terminal state.
    # Fires for every job update but no-ops cheaply unless the job carries the
    # transient marker.
    events.bind(
        "jobs.job.update.after",
        "girder_volview.backend.routes",
        inputs._cleanupTransientOnJobDone,
    )
    # Record each finalized output file's id onto the job that OWNS the file's
    # private parent folder, keyed by output identifier, so result collection
    # reads ids OFF the job. Fires for every upload but returns early unless the
    # upload lands in a job's output folder under a declared identifier.
    events.bind(
        "model.file.finalizeUpload.after",
        "girder_volview.backend.outputs",
        outputs._recordJobOutput,
    )
    # Ownership cascade: each job owns one private output folder + its staged
    # inputs. Running before the DB delete, this refuses to remove a nonterminal
    # owned job and cascade-deletes its owned resources, so the DELETE route,
    # Girder's built-in job route, and any direct Job.remove caller all
    # honor the same terminal guard and cleanup.
    events.bind(
        "model.job.remove",
        "girder_volview.backend.outputs",
        outputs._cascadeDeleteJobOwnedResources,
    )
    # Reverse ownership cascade: deleting a job's output folder in the Girder
    # hierarchy deletes the job record too (refusing for a live job), so folder
    # deletion is a first-class "delete this job" gesture and no orphaned job
    # rows accumulate. Removing the volview-jobs container recurses per job
    # folder.
    events.bind(
        "model.folder.remove",
        "girder_volview.backend.outputs",
        outputs._cascadeDeleteFolderOwnedJob,
    )
    # REST pre-guard for the same invariant: Folder.remove cleans contents
    # BEFORE model.folder.remove fires, so refuse a live job's folder (or a
    # container holding one) before the delete handler touches anything.
    events.bind(
        "rest.delete.folder/:id.before",
        "girder_volview.backend.outputs",
        outputs._refuseLiveJobFolderRestDelete,
    )
    # The same preflight for every OTHER recursive deletion entry point:
    # collection delete, user delete, and the batch /resource route all reach
    # Folder.remove without passing DELETE /folder/:id, so each would otherwise
    # destroy a live job's staged inputs and partial outputs before the late
    # model-level guard could refuse.
    events.bind(
        "rest.delete.collection/:id.before",
        "girder_volview.backend.outputs",
        outputs._refuseLiveJobCollectionRestDelete,
    )
    events.bind(
        "rest.delete.user/:id.before",
        "girder_volview.backend.outputs",
        outputs._refuseLiveJobUserRestDelete,
    )
    events.bind(
        "rest.delete.resource.before",
        "girder_volview.backend.outputs",
        outputs._refuseLiveJobResourceRestDelete,
    )
    # The recorded id map is READ-exposed; the job's own ACL is the gate
    # (otherFields + exposeFields, mirroring slicer_cli_web's slicerCLIBindings).

    girder_job.Job().exposeFields(
        level=AccessType.READ, fields={outputs._OUTPUTS_FIELD}
    )
    info["apiRoot"].folder.route(
        "GET", (":folderId", PROCESSING_ROUTE_NAME, "tasks"), listTasks
    )
    # Job re-discovery is context-scoped (it takes a launch folder), not
    # job-addressed: a reloaded client GETs this to re-find its jobs.
    info["apiRoot"].folder.route(
        "GET", (":folderId", PROCESSING_ROUTE_NAME, "jobs"), listJobHistory
    )
    info["apiRoot"].folder.route(
        "POST", (":folderId", PROCESSING_ROUTE_NAME, "stage"), stageInput
    )
    info["apiRoot"].folder.route(
        "GET",
        (":folderId", PROCESSING_ROUTE_NAME, "tasks", ":taskId", "spec"),
        getTaskSpec,
    )
    info["apiRoot"].folder.route(
        "POST",
        (":folderId", PROCESSING_ROUTE_NAME, "tasks", ":taskId", "run"),
        runTask,
    )
    info["apiRoot"].volview_processing = _JobResource()
