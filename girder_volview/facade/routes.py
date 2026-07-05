"""Processing facade -- REST routes + job creation + route registration.

Split out of the former monolith ``processing.py`` (Chunk 32, pure code motion).
This module owns the wire surface: the ``@boundHandler`` route functions, the
single live slicer_cli_web job-creation touch point (``_genDockerJob``), and
``addProcessingRoutes`` (event bindings + route table).

Cross-module helper calls are MODULE-QUALIFIED on purpose (``submit._foo`` /
``inputs._foo`` / ``outputs._foo`` / ``results._foo``) so a test that patches a
helper on its DEFINING module reaches the call site here (Chunk 32 monkeypatch-
target contract) — a bare ``from .submit import _foo`` would bind a name that a
later ``setattr(submit, "_foo", ...)`` could not reach.
"""

import copy

import cherrypy
from girder import events, logger
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource, boundHandler
from girder.constants import AccessType, TokenScope
from girder.exceptions import GirderException, RestException, ValidationException
from girder.models.folder import Folder

from ..csrf import csrfProtect
from ..utils import makeFileDownloadUrl
from .slicer_spec import translate_slicer_xml
from . import inputs, submit, outputs, results


# ---------------------------------------------------------------------------
# Launch-context routes (folder-addressed)
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
    if user and submit._slicerCliAvailable():
        try:
            tasks.extend(
                [submit._cliItemToSummary(c) for c in submit._scopedCliItems(user)]
            )
        except Exception:
            logger.exception("Failed to list slicer_cli_web items")
    return tasks


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("List this launch context's recent processing jobs (tier-2).")
    .notes(
        "Tier-2 cold-reload re-discovery (Chunk 19, D5): a reloaded client "
        "re-finds jobs from a previous page life and re-attaches via the same "
        "poll/results path. Unbounded + context-scoped -- every facade job "
        "stamped with THIS launch folder, no time window (an old in-context job "
        "is still listed); `since`/paging stay transport details. Returns "
        "NeutralJobHandle[] -- the client never sees the route, the JobStatus "
        "enum, or a file id."
    )
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .produces(["application/json"])
)
def listRecentJobs(self, folder):
    # Scoped by the submitting user (auth) AND the launch-folder stamp: Girder
    # jobs are user-owned, not folder-linked, so the launch context recorded on
    # the job at submit (`inputs._stampJobContext`) is the only handle on "this
    # study's jobs". Unbounded (limit=0, no since cutoff -- D5).
    user = self.getCurrentUser()
    if not user:
        return []
    from girder.constants import SortDir
    from girder_jobs.models.job import Job as JobModel
    cursor = JobModel().findWithPermissions(
        query={inputs._LAUNCH_FOLDER_FIELD: str(folder["_id"])},
        user=user,
        jobUser=user,
        level=AccessType.READ,
        limit=0,
        sort=[("created", SortDir.DESCENDING)],
    )
    return [results._projectJobHandle(job) for job in cursor]


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
    if not submit._slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=404)
    cliItem = submit._findScopedCliItem(taskId, user)
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
    # Scope-limit the container token to the data plane the CLI actually needs:
    # read its inputs, write its outputs (Chunk 21, D9 addendum). Narrower than
    # the ecosystem norm (slicer_cli_web mints full-auth tokens) without weakening
    # Girder ACLs — the submitter's own read/write reach still bounds it.
    token = Token().createToken(
        user=user, scope=[TokenScope.DATA_READ, TokenScope.DATA_WRITE]
    )
    # Inject the CLI's `girderApiUrl`/`girderToken` params so slicer_cli_web feeds
    # the container its API URL + token (the b3 convention). slicer_cli_web only
    # substitutes its GirderApiUrl()/GirderToken() runtime transforms when these
    # keys are present in the params it processes (prepare_task
    # `_add_optional_input_param` skips a param absent from args); the REST route
    # would default them in, but the facade calls `subHandler` directly, so we
    # supply them here. Empty -> slicer_cli_web substitutes the transforms, and
    # GirderToken resolves to THIS scoped token (Ch21), not a broader one. Without
    # them a CLI that fetches its own inputs by id (`reference="_girder_id_"`, e.g.
    # a multi-file DICOM series) has no way to reach Girder. Harmless for a CLI
    # that declares neither -- slicer_cli_web ignores undeclared args.
    params = dict(params)
    params.setdefault("girderApiUrl", "")
    params.setdefault("girderToken", "")
    # Capture the EXACT per-hook DATA_WRITE tokens girder_worker_utils mints WHILE
    # subHandler builds the result-hooks (a fresh token per hook — the worker uploads
    # each output under one of THOSE, never `token`; girder_io.py). The recorder
    # intercepts Token.createToken on THIS request thread only, so a concurrent
    # same-user/same-task submit (running on another thread) can never bleed its tokens
    # in — killing the old [since,until] time-window race. subHandler is synchronous.
    with outputs._captureUploadTokens() as capturedTokenIds:
        # Take a copy so the handler can mutate freely.
        job_obj = handler.subHandler(cliItem, copy.deepcopy(params), user, token)
    job = job_obj.job if hasattr(job_obj, "job") else job_obj
    # Reference-bound outputs (D5): record the declared output specs + the set of
    # tokens an output upload may arrive under so the data.process handler can
    # correlate each uploaded output back to THIS job and record its file id keyed by
    # output identifier.
    outputs._bindJobOutputs(job, token, cliItem.xml, capturedTokenIds)
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

    # Submit-boundary input validation: reject a crafted payload that carries
    # reserved credentials or an undeclared output-folder param (Chunk 21). Runs
    # before any task lookup or work, and composes with the @csrfProtect guard.
    submit._rejectReservedSubmitParams(values)

    if not submit._slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=500)

    cliItem = submit._findScopedCliItem(taskId, user)
    if not cliItem:
        raise RestException("Unknown taskId", code=404)

    # Auto-generate a deterministic output filename for any output param the user
    # didn't fill (input file + CLI name + parameter name + extension). No longer
    # uniquified via a folder scan: outputs bind to the job by reference, not name
    # (D5), so a duplicate filename in the folder can no longer cross results.
    values = submit._autofillOutputs(dict(values), cliItem.xml, cliItem.name)

    # Resolve each bound input's client-minted URIs back to file ids (own-scheme
    # validation + per-user ACL re-check) and forward the ids to the CLI (b3).
    params = submit._translateValuesToSlicerParams(values, user, folder)
    # A bound input that was staged (its item tagged transient) is recorded on the
    # job so inputs._cleanupTransientOnJobDone deletes it at terminal state (Ch14).
    transientItemIds = inputs._collectTransientInputItemIds(values, user)
    logger.info(
        "[volview_processing] runTask folder=%s task=%s params=%s",
        folder["_id"], taskId, params,
    )

    job_doc = _genDockerJob(cliItem, params, user)
    # Stamp the launch context + the handle's inputs so a reloaded client can
    # re-discover this job (listRecentJobs) and re-associate its results by
    # provenance (Chunk 19, D5). The URIs are the bound inputs' verbatim opaque
    # URIs, read off the submitted values (before autofill only added output
    # filename strings, which carry no `uris`).
    inputs._stampJobContext(job_doc, folder, taskId, inputs._collectInputUris(values))
    if transientItemIds:
        inputs._markJobTransients(job_doc, transientItemIds)
    return {"jobId": str(job_doc["_id"])}


# ---------------------------------------------------------------------------
# Job-addressed routes (D5) — status / results / cancel are keyed by job id
# alone and gated by the job's OWN ACL. The launch folder is not part of a job's
# identity, so these carry no ``folderId`` (they live on the folder-free
# ``volview_processing`` resource below, not the folder tree the launch-context
# routes use). getJob / getJobResults are READ-gated; cancel is WRITE-gated so a
# read-only viewer who can see a job's status cannot cancel it.
# ---------------------------------------------------------------------------

@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get job status.")
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJob(self, jobId):
    user = self.getCurrentUser()
    from girder_jobs.models.job import Job as JobModel
    job = JobModel().load(jobId, user=user, level=AccessType.READ, exc=True)
    return results._projectJobStatus(job)


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get job results.")
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJobResults(self, jobId):
    user = self.getCurrentUser()
    from girder_jobs.models.job import Job as JobModel
    job = JobModel().load(jobId, user=user, level=AccessType.READ, exc=True)
    return results._jobResultsPayload(job, user)


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@csrfProtect
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
    # WRITE-gated load: a read-only user (who can GET the status) is blocked here.
    user = self.getCurrentUser()
    from girder_jobs.models.job import Job as JobModel
    jobModel = JobModel()
    job = jobModel.load(jobId, user=user, level=AccessType.WRITE, exc=True)
    try:
        jobModel.cancelJob(job)
    except ValidationException:
        # Best-effort: Girder refuses a CANCELED transition from a terminal state,
        # so an already-finished job simply cannot be cancelled. That is not an
        # error here -- we fall through and report the job's real state below
        # rather than fabricate a `cancelled` the poller would contradict.
        pass
    # Reload fresh and project the ACTUAL persisted state (contract Seam 3
    # best-effort): the client's poller converges on whatever Girder holds.
    fresh = jobModel.load(jobId, user=user, level=AccessType.READ, exc=True)
    return results._projectJobStatus(fresh)


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
    inputs._sweepOrphanTransients(folder)
    size = int(cherrypy.request.headers.get("Content-Length") or 0)
    if size <= 0:
        raise GirderException(
            "Expected non-zero Content-Length header",
            "girder.api.v1.folder.volview_stage",
        )
    fileDoc = inputs._streamBodyIntoItem(folder, user, size, name or "staged")
    inputs._tagItemTransient(fileDoc, user)
    # The facade mints the URI; the client constructs none. Type-agnostic shape:
    # the semantic `type` tag is the client's to add at submit, not ours.
    return {"uris": [makeFileDownloadUrl(fileDoc)]}


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

class _JobResource(Resource):
    """Folder-free REST surface for the job-addressed routes (D5).

    Mounted at ``/volview_processing`` (a sibling of ``/folder``, ``/item``),
    this hosts status / results / cancel keyed by job id alone -- the launch
    folder is not part of a job's identity. The launch-context routes
    (tasks / spec / run / stage) stay on the folder tree because they genuinely
    operate per-folder. The handlers are the same module-level ``@boundHandler``
    functions; only their mount point differs.
    """

    def __init__(self):
        super().__init__()
        self.resourceName = "volview_processing"
        self.route("GET", ("jobs", ":jobId"), getJob)
        self.route("GET", ("jobs", ":jobId", "results"), getJobResults)
        self.route("POST", ("jobs", ":jobId", "cancel"), cancelJob)


def addProcessingRoutes(info):
    # Install the upload-token recorder once (fix #4): wraps Token.createToken so a
    # submit captures the EXACT per-hook upload tokens girder_worker_utils mints on its
    # request thread (outputs._captureUploadTokens), replacing the racy time-window
    # capture. Idempotent + a no-op outside a capture block.
    outputs._installUploadTokenRecorder()
    # Delete a job's transient staged inputs once it reaches a terminal state
    # (Chunk 14). Bound once at plugin load; fires for every job update but no-ops
    # cheaply unless the job carries the transient marker.
    events.bind(
        "jobs.job.update.after",
        "girder_volview.processing",
        inputs._cleanupTransientOnJobDone,
    )
    # Reference-bound job outputs (D5): record each uploaded output file's id onto
    # its originating job, keyed by output identifier, so result collection reads
    # ids OFF the job instead of scanning the launch folder by name. Fires for
    # every upload but returns early unless the upload carries an output reference
    # correlated to a facade job (fail closed).
    events.bind(
        "data.process",
        "girder_volview.processing.outputs",
        outputs._recordJobOutput,
    )
    # The recorded id map is READ-exposed; the job's own ACL is the gate (D5 —
    # otherFields + exposeFields, mirroring slicer_cli_web's slicerCLIBindings).
    from girder_jobs.models.job import Job as JobModel
    JobModel().exposeFields(level=AccessType.READ, fields={outputs._OUTPUTS_FIELD})
    info["apiRoot"].folder.route(
        "GET", (":folderId", "volview_processing", "tasks"), listTasks
    )
    # Tier-2 cold-reload re-discovery (Chunk 19, D5): context-scoped like
    # listTasks/runTask/stage (it takes a launch folder), NOT job-addressed like
    # status/results/cancel. A reloaded client GETs this to re-find its jobs.
    info["apiRoot"].folder.route(
        "GET", (":folderId", "volview_processing", "jobs"), listRecentJobs
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
    # Job-addressed routes (D5) live on a dedicated folder-free resource: a job's
    # status/results/cancel are keyed by job id alone (gated by the job's own
    # ACL), so they must NOT hang off the folder tree. Greenfield -- no
    # folder-scoped compat shim for the old shape.
    info["apiRoot"].volview_processing = _JobResource()
