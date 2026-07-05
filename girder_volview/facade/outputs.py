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
    hooks (``_capturedUploadTokens``, captured tightly around the ``subHandler`` call),
    and ``_jobForOutputUpload`` matches that set — narrowed by the declared
    ``identifier`` so overlapping same-user submits never cross.
  * ``results._collectJobResults`` reads those ids OFF the job. No name is ever
    matched, so the race is gone.
"""

import json

from girder import logger
from girder.constants import TokenScope
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
    (``_capturedUploadTokens``). The match is narrowed by the reference ``identifier``
    against the job's declared output specs, so two overlapping same-user submits whose
    capture windows touch can never cross (a token in both sets still resolves to the job
    that actually declares that output). A facade-minted reference may also carry the job
    id directly; it is honored when present. Returns the job doc, or ``None`` (fail closed
    — an uncorrelated upload is never recorded onto some other job).
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
    map the handler fills in. ``uploadTokens`` are the per-hook tokens captured around
    ``subHandler`` (``_capturedUploadTokens``); the facade's own ``token`` is always
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


def _capturedUploadTokens(user, since, until):
    """Ids of the DATA_WRITE tokens minted for ``user`` during job build (D5 fix).

    girder_worker_utils' ``GirderClientTransform`` mints a *fresh* DATA_READ/DATA_WRITE
    token per result-hook GirderClient (``girder_io.py``) and the worker uploads each
    output under one of those — never the facade's own job token. ``routes._genDockerJob``
    captures them by querying the tokens this user gained over the (tight) ``subHandler``
    window; ``_bindJobOutputs`` records them so ``_recordJobOutput`` can correlate the
    upload back to this job. Best-effort: a capture failure must never fail a submit —
    results simply won't auto-attach (the pre-fix behavior), never a 500.

    The window is scoped to this user + DATA_WRITE scope + [since, until]; extra tokens
    swept in are harmless (they only matter if an output arrives under them). The one
    residual is two *same-task* same-user submits whose windows overlap — identifier
    narrowing in ``_jobForOutputUpload`` disambiguates every other concurrent case.
    """
    userId = user.get("_id") if isinstance(user, dict) else getattr(user, "_id", None)
    if userId is None:
        return []
    from girder.models.token import Token
    try:
        cursor = Token().find({
            "userId": userId,
            "scope": TokenScope.DATA_WRITE,
            "created": {"$gte": since, "$lte": until},
        })
        return [str(doc["_id"]) for doc in cursor]
    except Exception:
        logger.exception("Failed to capture upload tokens for user %s", userId)
        return []
