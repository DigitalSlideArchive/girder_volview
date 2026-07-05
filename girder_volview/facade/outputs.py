"""Processing facade -- reference-bound job-output correlation (the write path).

Split out of the former monolith ``processing.py`` (Chunk 32, pure code motion).
This module owns the ``data.process`` side of Seam 3: recording, onto the job
that produced them, the file ids ``girder_worker`` uploads -- bound by REFERENCE
(the slicer_cli_web output ``identifier`` + the per-run upload token), never by
filename. The read path (projecting those recorded ids into result intents) and
status projection live in ``results.py``.

Reference-bound job outputs (D5) — outputs bind to the job by reference, never
by filename:

The old collector name-scanned the launch folder for an item matching the
output filename recorded in ``Job._original_params`` — so two concurrent jobs
writing the same output name into the same folder could cross results, and two
simultaneous "unique name" picks could collide. This replaces name-matching
with the ecosystem's reference→job binding (slicer_cli_web ``girder_plugin.py``
``_onUpload`` prior art, generalized to N outputs):

  * At submit, ``_bindJobOutputs`` records on the job (plain ``otherFields`` — no
    schema change) the declared output specs and the set of tokens an output
    upload may arrive under (see next bullet).
  * Each output file ``girder_worker`` uploads carries slicer_cli_web's per-run
    reference (``prepare_task.py`` stamps the output ``identifier`` + a per-run
    ``uuid`` on every ``GirderUploadVolumePathToFolder`` hook). The ``data.process``
    handler ``_recordJobOutput`` correlates that upload back to THIS job by the
    token it arrives under, and records the file id keyed by ``identifier`` (dotted
    ``$set`` key → each output binds under its own key, so N outputs never
    overwrite, unlike slicer_cli_web's single ``slicerCLIBindings.outputs.parameters``).
  * That upload token is NOT the facade's own job token: girder_worker_utils'
    ``GirderClientTransform`` mints a *fresh* DATA_WRITE token per result-hook
    GirderClient (``girder_io.py``) and the worker uploads each output under one of
    THOSE. So ``_bindJobOutputs`` records the SET of tokens minted while building the
    hooks (``_captureUploadTokens`` intercepts ``Token.createToken`` on this submit's
    thread, so the set is EXACTLY this job's hook tokens — two concurrent same-user
    same-task submits capture disjoint sets and never cross), and
    ``_jobForOutputUpload`` matches that set, narrowed by the declared ``identifier``.
  * ``results._collectJobResults`` reads those ids OFF the job. No name is ever
    matched, so the race is gone.
"""

import contextlib
import json
import threading

from girder import logger
from girder.models.item import Item

from ..utils import JOB_OUTPUT_META_KEY
from .slicer_spec import parse_cli

# Facade-owned job fields (otherFields, not a schema change — D5). The id map is
# READ-exposed (``routes.addProcessingRoutes``); the job's own ACL is the gate.
_OUTPUTS_FIELD = "volviewOutputs"          # {identifier: str(fileId)}
_OUTPUT_SPECS_FIELD = "volviewOutputSpecs"  # [{name, tag, isLabel, fileExtensions}]
_JOB_TOKEN_FIELD = "volviewJobToken"       # str(token _id) — facade's own job token
_JOB_TOKENS_FIELD = "volviewJobTokens"     # [str(token _id)] — every token an output
#                                            upload may arrive under (the facade token
#                                            PLUS the per-hook tokens girder_worker_utils
#                                            mints); the real data.process correlation key


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

    Correlation is by the token the upload arrives under. girder_worker does NOT
    upload under the facade's own job token: girder_worker_utils' ``GirderClientTransform``
    mints a fresh DATA_WRITE token per result-hook and uploads each output under one
    of those, so ``_bindJobOutputs`` records them all as ``_JOB_TOKENS_FIELD`` at submit
    (``_captureUploadTokens``, which captures the EXACT tokens minted on this submit's
    thread — see ``_installUploadTokenRecorder``). The match is narrowed by the reference
    ``identifier`` against the job's declared output specs. Returns the job doc, or
    ``None`` (fail closed — an uncorrelated upload is never recorded onto some other job).

    The upload ``reference`` is caller-controllable (Girder passes a POST-supplied
    ``reference`` straight into ``data.process``), so it is NEVER trusted to name its own
    job: correlation is solely by the server-minted, user-bound upload token. (A prior
    ``jobId``-in-reference shortcut was removed — it force-loaded an arbitrary job past
    the ACL, letting a crafted upload attach a file onto any job.)
    """
    from girder_jobs.models.job import Job as JobModel
    token = info.get("currentToken")
    tokenId = token.get("_id") if isinstance(token, dict) else None
    if not tokenId:
        return None
    tokenId = str(tokenId)
    # Primary: the token is one of the per-hook upload tokens captured at submit,
    # narrowed by the declared output identifier.
    query = {_JOB_TOKENS_FIELD: tokenId}
    identifier = ref.get("identifier")
    if isinstance(identifier, str) and identifier:
        query["%s.name" % _OUTPUT_SPECS_FIELD] = identifier
    job = JobModel().findOne(query)
    if isinstance(job, dict):
        return job
    # Back-compat: a job stamped before token-set capture carried only the single
    # facade token (its data.process events were synthesized under that same token).
    return JobModel().findOne({_JOB_TOKEN_FIELD: tokenId})


def _tagJobOutputItem(fileDoc):
    """Mark a job-output file's parent item so the launch manifest excludes it.

    Best-effort, idempotent (a re-fired upload just re-sets the marker). Loaded
    ``force=True`` because this runs in the ``data.process`` daemon context; the
    marker is item metadata (``volviewJobOutput``), mirroring the ``volviewTransient``
    tag pattern. Failures are logged, never raised — an untaggable output must not
    disrupt the upload daemon (the file is still recorded on the job).
    """
    itemId = fileDoc.get("itemId") if isinstance(fileDoc, dict) else None
    if not itemId:
        return
    try:
        item = Item().load(itemId, force=True, exc=False)
        if item:
            Item().setMetadata(item, {JOB_OUTPUT_META_KEY: True})
    except Exception:
        logger.exception("Failed to tag job-output item %s", itemId)


def _recordJobOutput(event):
    """``data.process`` handler: record an uploaded output file id onto its job (D5).

    Each output file ``girder_worker`` uploads for a facade job carries
    slicer_cli_web's reference (the output ``identifier``); this handler correlates
    the upload back to the originating job (``_jobForOutputUpload``) and records the
    file id under that identifier (``otherFields`` dotted key → Mongo ``$set`` nests
    per identifier, so N outputs each bind without overwriting). Fail closed: a
    foreign or uncorrelated upload is ignored. ``results._collectJobResults`` later
    reads these ids OFF the job, so no output is ever resolved by folder-name match.
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
    # Mark the output file's item so the launch manifest excludes it (Chunk 19,
    # D5): job results take the JOB path only. The file stays durable in the
    # folder (re-fetched via the job) but VolView's native loadSegmentations
    # never also grabs it (which would double-apply with no shared dedup key).
    _tagJobOutputItem(fileDoc)


def _bindJobOutputs(job, token, cli_xml, uploadTokens=None):
    """Record on the job everything reference-bound collection needs (D5).

    The declared output specs (so collection needs no CLIItem lookup and no
    ``_original_params``), the set of tokens an output upload may arrive under (the
    ``data.process`` correlation key — see ``_jobForOutputUpload``), and an empty id
    map the handler fills in. ``uploadTokens`` are the per-hook tokens captured during
    ``subHandler`` (``_captureUploadTokens``); the facade's own ``token`` is always
    included first so a synthesized event under it (offline tests, older callers) still
    correlates. The declared output specs come from the single
    ``slicer_spec.parse_cli`` walk. Split out from ``routes._genDockerJob`` so it is
    unit-testable without slicer_cli_web: a pure ``updateJob`` write, the same
    otherFields-on-job pattern ``inputs._markJobTransients`` uses. Not a schema change.
    """
    from girder_jobs.models.job import Job as JobModel
    specs = parse_cli(cli_xml or "")["outputs"]
    tokens = [str(token["_id"])]
    for extra in (uploadTokens or []):
        extra = str(extra)
        if extra not in tokens:
            tokens.append(extra)
    try:
        JobModel().updateJob(job, otherFields={
            _OUTPUT_SPECS_FIELD: specs,
            _JOB_TOKEN_FIELD: str(token["_id"]),
            _JOB_TOKENS_FIELD: tokens,
            _OUTPUTS_FIELD: {},
        })
    except Exception:
        logger.exception("Failed to bind outputs on job %s", job.get("_id"))


# Thread-local bucket: while a submit is inside a ``_captureUploadTokens`` block on
# this request thread, ``.ids`` is a list the wrapped ``Token.createToken`` appends
# each minted token id to. ``None`` (the default) means "not recording", so the
# wrapper is a no-op for every other token mint process-wide.
_tokenCapture = threading.local()


def _installUploadTokenRecorder():
    """Wrap ``Token.createToken`` ONCE so a submit can capture the EXACT upload tokens
    girder_worker_utils mints per result-hook (``girder_io.py``) during its ``subHandler``
    call — on THIS request thread only.

    This replaces the old ``[since, until]`` time-window query, whose one residual race
    was that two same-user *same-task* submits with overlapping windows captured each
    other's tokens (and identical output identifiers could not disambiguate them). The
    exact per-hook token is not recoverable from the created job document (girder_worker
    stores only args/kwargs/celeryTaskId, never the result-hook tokens), so we intercept
    the mint itself. Two concurrent submits run on different request threads, each with
    its own ``threading.local`` bucket, so their captured sets are DISJOINT by
    construction — the race is gone regardless of identifier overlap or worker-process
    topology.

    Idempotent (guarded by ``_volviewUploadRecorder``) and a no-op for every mint outside
    a capture block, so it never changes global token behavior. Installed once at plugin
    load (``routes.addProcessingRoutes``).
    """
    from girder.models.token import Token
    if getattr(Token, "_volviewUploadRecorder", False):
        return
    _original = Token.createToken

    def createToken(self, *args, **kwargs):
        tok = _original(self, *args, **kwargs)
        bucket = getattr(_tokenCapture, "ids", None)
        if bucket is not None:
            try:
                bucket.append(str(tok["_id"]))
            except Exception:
                pass
        return tok

    Token.createToken = createToken
    Token._volviewUploadRecorder = True


@contextlib.contextmanager
def _captureUploadTokens():
    """Yield a list that collects ids of every token minted on THIS thread within the block.

    ``routes._genDockerJob`` wraps its synchronous ``subHandler`` call in this block so
    the yielded list holds exactly the per-hook upload tokens girder_worker_utils minted
    for THIS job (plus the harmless ``rest.create_job`` token — no output ever uploads
    under it). Requires ``_installUploadTokenRecorder`` to have wrapped ``Token.createToken``;
    if it has not, the list is simply empty (results won't auto-attach — the pre-fix
    behavior, never a 500). The list stays valid after the block: only the thread-local
    pointer is cleared, so a caller may read it once the recorder is torn down.
    """
    ids = []
    _tokenCapture.ids = ids
    try:
        yield ids
    finally:
        _tokenCapture.ids = None
