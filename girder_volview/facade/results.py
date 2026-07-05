"""Processing facade -- job status projection + result collection (the read path).

Split out of the former monolith ``processing.py`` (Chunk 32, pure code motion).
This module owns the neutral projections the client polls and applies:

- **Status projection**: Girder ``JobStatus`` → the contract's neutral job
  state / status / handle shapes (never the girder enum on the wire).
- **Result collection**: reading the file ids ``outputs._recordJobOutput``
  recorded ON the job (reference-bound, never a folder-name scan) and projecting
  each into its declarative result intent (contract Seam 2), inside the honest
  result-read envelope (D5).
"""

import datetime

from girder import logger
from girder.constants import AccessType
from girder.exceptions import RestException
from girder.models.file import File

from ..utils import makeFileDownloadUrl, _toIso
from .inputs import _INPUT_URIS_FIELD, _TASK_ID_FIELD
from .outputs import _OUTPUTS_FIELD, _OUTPUT_SPECS_FIELD


# ---------------------------------------------------------------------------
# Status projection — the neutral JobStatus → state map (never the girder enum)
# ---------------------------------------------------------------------------

def _projectJobState(job):
    """The neutral projected job state (a ``jobStateSchema`` value) from Girder's
    ``JobStatus``.

    The single shared JobStatus->state map, read by BOTH the status projection
    (``_projectJobStatus``) and the tier-2 handle projection
    (``_projectJobHandle``) — so a handle's ``state`` can never diverge from the
    status the client polls. A job with no ``status`` maps to ``"pending"`` (fail
    closed). Neutral names only — never the girder ``JobStatus`` enum on the wire.
    """
    from girder_jobs.constants import JobStatus
    state_map = {
        JobStatus.INACTIVE: "pending",
        JobStatus.QUEUED: "pending",
        JobStatus.RUNNING: "running",
        JobStatus.SUCCESS: "success",
        JobStatus.ERROR: "error",
        JobStatus.CANCELED: "cancelled",
    }
    return state_map.get(job.get("status"), "pending")


def _projectJobStatus(job):
    """Convert Girder Job status to ProcessingJobStatus."""
    state = _projectJobState(job)
    # Race guard (fix #2): a job can read terminal SUCCESS while its output-record
    # events are still queued on Girder's async data.process daemon, so a results
    # read here would return {intents:[], missing:0} -- indistinguishable from a
    # genuine empty success -- and the client would stop polling. Hold the
    # projected status non-terminal ("running") until every declared output has
    # drained, so the client's existing poll loop keeps polling (no client change)
    # and only fires completion once /results can actually return the intents. A
    # racy direct /results call during drain still hits the explicit 400 gate in
    # _jobResultsPayload (which also calls this), a free secondary defense.
    if state == "success" and _outputsStillDraining(job):
        return {"jobId": str(job["_id"]), "state": "running"}
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


def _projectFinishedAt(job):
    """The neutral terminal instant of a job (server clock), or ``""``.

    Projected from Girder's job status-transition timestamps (``_updateStatus``
    pushes ``{status, time}`` on every transition): the ``time`` of the most
    recent transition into a terminal state. A still-running / never-terminal job
    has no such entry and yields ``""`` — the client applies results only for a
    terminal-succeeded job anyway (result reads gate on terminal success), and
    the empty instant sorts before any real watermark. ISO-8601 UTC so the client
    compares it to ``sessionSavedAt`` as UTC instants (D5).
    """
    from girder_jobs.constants import JobStatus
    terminal = {JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED}
    timestamps = job.get("timestamps") or []
    for ts in reversed(timestamps):
        if isinstance(ts, dict) and ts.get("status") in terminal:
            return _toIso(ts.get("time")) or ""
    return ""


def _projectJobHandle(job):
    """Project a facade job into a neutral ``NeutralJobHandle`` (Chunk 19, D5).

    ``{jobId, taskId, inputUris, finishedAt, state}`` — the SAME neutral shape the
    ``processing-contract`` golden fixture pins (the client never sees the Girder
    route, the ``JobStatus`` enum, or a file id). ``taskId`` + ``inputUris`` are
    read straight off the launch-context stamp; ``finishedAt`` is projected from
    the job's terminal timestamp. The ``inputUris`` re-associate results to the
    reloaded scene by matching the client's OWN provenance (Seam 1).

    ``state`` (Chunk 27, tier-2 reload economy) is the neutral projected job state
    from the SAME shared map ``_projectJobStatus`` uses, so a client re-discovering
    a terminal-non-success handle records it WITHOUT a ``getJob`` round-trip. It is
    an OPTIONAL contract field; the reference facade always emits it, and a
    pre-upgrade facade that omits it stays wire-compatible.
    """
    inputUris = job.get(_INPUT_URIS_FIELD)
    return {
        "jobId": str(job["_id"]),
        "taskId": str(job.get(_TASK_ID_FIELD) or ""),
        "inputUris": list(inputUris) if isinstance(inputUris, list) else [],
        "finishedAt": _projectFinishedAt(job),
        "state": _projectJobState(job),
    }


# ---------------------------------------------------------------------------
# Result intents + collection (Seam 2 / Seam 3, D3/D5) — reference-bound reads
# ---------------------------------------------------------------------------

def _intentForOutput(out, url, name, jobId):
    """Build the declarative result intent for one output (contract Seam 2, D3).

    Results cross the wire as declarative intents the client's single applier
    applies — never a ``role`` the client switches on. The v1 vocabulary the
    client validates (VolView ``processing-contract/wire.ts``): a labelmap →
    ``add-segment-group``, a plain image → ``add-base-image``, any other file →
    ``download``. No CLI declares a state output yet, so ``restore-state`` has no
    producer here.

    A labelmap intent carries a ``source: {jobId, outputId}`` provenance tag
    (``outputId`` = the CLI's output identifier) so the created segment group
    round-trips the ``.volview.zip`` (tier-2 idempotency key, Chunk 19). A
    labelmap's segment names/colors travel *inside* the ``.seg.nrrd`` file as
    embedded metadata (Chunk 34) and are read client-side, so the facade sets no
    ``segments`` payload (the wire field stays optional — a producer that embeds
    its metadata simply never sets it). Validates against the contract
    ``result-intent`` schema.
    """
    fileRef = {"url": url, "name": name}
    if out["isLabel"]:
        return {
            "intent": "add-segment-group",
            **fileRef,
            "source": {"jobId": str(jobId), "outputId": out["name"]},
        }
    if out["tag"] == "image":
        return {"intent": "add-base-image", **fileRef}
    return {"intent": "download", **fileRef}


def _recordedJobOutputs(job):
    """The ``{identifier: fileId}`` map the ``data.process`` handler recorded (or {})."""
    outputs = (job or {}).get(_OUTPUTS_FIELD)
    return dict(outputs) if isinstance(outputs, dict) else {}


def _recordedOutputSpecs(job):
    """The declared output specs recorded at submit (or [])."""
    specs = (job or {}).get(_OUTPUT_SPECS_FIELD)
    return list(specs) if isinstance(specs, list) else []


# Grace window: how long after the SUCCESS transition to treat un-recorded
# declared outputs as "still draining" on Girder's async data.process daemon
# before accepting the job as genuinely done. The daemon normally drains in well
# under a second; the window only bounds the pathological "declared output never
# written" case so it can never pin the client in an infinite poll.
_OUTPUT_DRAIN_GRACE_SECONDS = 30


def _terminalTime(job):
    """The datetime of the job's most recent terminal status transition, or None."""
    from girder_jobs.constants import JobStatus
    terminal = {JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED}
    for ts in reversed(job.get("timestamps") or []):
        if isinstance(ts, dict) and ts.get("status") in terminal:
            return ts.get("time")
    return None


def _outputsStillDraining(job):
    """A SUCCESS job whose async data.process output-record events are still queued.

    True iff the job succeeded but at least one DECLARED projectable output spec
    (``volviewOutputSpecs``) has not yet been recorded into ``volviewOutputs``.
    Keyed on declared spec NAMES only, so a recorded-but-undeclared identifier
    (slicer's ``returnparameterfile``) -- which lives only on the recorded side --
    never counts as pending. Bounded by a grace window after the terminal
    transition so an output the CLI never writes cannot hold the client in an
    infinite poll.

    Used by ``_projectJobStatus`` to hold the poll status non-terminal while the
    outputs drain: without it, a job read as terminal-SUCCESS during the drain
    window would collect ``{intents:[], missing:0}`` -- indistinguishable from a
    genuine empty success -- and the client would stop polling and never see the
    real outputs.
    """
    from girder_jobs.constants import JobStatus
    if job.get("status") != JobStatus.SUCCESS:
        return False
    specs = [
        s for s in _recordedOutputSpecs(job)
        if isinstance(s, dict) and s.get("name")
    ]
    if not specs:
        return False  # nothing declared -> genuine empty success, never "draining"
    recorded = _recordedJobOutputs(job)
    if all(s["name"] in recorded for s in specs):
        return False  # every declared output recorded -> genuinely done
    finished = _terminalTime(job)
    if not isinstance(finished, datetime.datetime):
        # A SUCCESS job normally carries its terminal timestamp (girder_jobs writes
        # the status $set and the timestamp $push atomically), so this is anomalous.
        # Resolve to the real state rather than hold "running" with no time bound
        # (a missing timestamp must never wedge the client in an infinite poll).
        return False
    # Girder's Mongo collections are tz-aware UTC, so a timestamp read back from the
    # DB is tz-aware while ``datetime.utcnow()`` is naive -- subtracting the two
    # raises TypeError (a 500 on the poll). Normalize both to aware-UTC, mirroring
    # ``utils._toIso``; a hand-built naive timestamp (offline tests) is treated UTC.
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc) - finished).total_seconds()
    return age < _OUTPUT_DRAIN_GRACE_SECONDS


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

    Reads the ``{identifier: fileId}`` map ``outputs._recordJobOutput`` recorded ON
    the job (never a folder-name scan, never ``_original_params``), loads each file
    under the submitting user's READ permission, and projects it into its result
    intent (contract Seam 2) with a ``makeFileDownloadUrl`` download url (origin-
    relative and filename-encoded — retiring the hand-built ``/api/v1/file/…``
    f-string that broke non-default API mounts). Returns ``(results, missing)`` where
    ``missing`` counts recorded outputs whose file is gone/unreadable — a deleted
    output is a countable loss, never a silently shorter list. Two concurrent same-
    name jobs can never cross results: each reads only the ids bound to itself.
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

    # Pass 2: project each resolved file into its declarative result intent
    # (contract Seam 2). The wire shape is the intent object itself — `{intent,
    # url, name, source?}` — plus the `id`/`mimeType`/`size` file metadata the
    # client's JobList reads. No `role`: the client applies the intent directly
    # and never switches on a role (D3/D4). A labelmap's segment names/colors
    # travel inside the `.seg.nrrd` file (Chunk 34) and are read client-side, so
    # the facade never content-sniffs an output or pairs a sidecar by position.
    results = []
    for entry in resolved:
        out = entry["out"]
        fileDoc = entry["fileDoc"]
        url = makeFileDownloadUrl(fileDoc)
        intent = _intentForOutput(out, url, fileDoc["name"], job["_id"])
        result = {
            **intent,
            "id": str(fileDoc["_id"]),
            "mimeType": fileDoc.get("mimeType"),
            "size": fileDoc.get("size"),
        }
        results.append(result)
    return results, missing


def _jobResultsPayload(job, user):
    """Apply honest result-read semantics (D5) and return the wire result envelope.

    - A non-succeeded job (failed / running / pending / cancelled) is an EXPLICIT
      error, never an empty list — the client (Chunk 12) gates reads on terminal
      success and treats this error as an error, not empty results.
    - A succeeded job whose recorded outputs ALL fail to resolve (every output file
      deleted) is likewise an error carrying the ``missing`` count, so "succeeded,
      outputs deleted" is distinguishable from "succeeded, no outputs".
    - Otherwise the resolved intents are returned inside the ``{intents, missing}``
      envelope (contract ``jobResultsSchema``, Seam 3 / Chunk 28): the SAME result
      items as before, wrapped, with the ``missing`` count of outputs that could
      not be resolved riding alongside (a partial loss returns what resolved and
      reports the rest, rather than silently dropping it). ``missing`` is 0 on a
      clean success.

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
    return {"intents": results, "missing": missing}
