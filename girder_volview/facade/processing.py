"""Processing facade -- provider config + back-compat re-export shim.

Chunk 32 split the former ~1670-line monolith into cohesive modules
(``inputs`` / ``submit`` / ``outputs`` / ``results`` / ``routes``, plus the
``slicer_spec`` XML parser). This module keeps the two things that genuinely
belong at the facade seam -- the per-launch **provider config** block the
launch manifest injects, and ``addProcessingRoutes`` (re-exported) -- and
re-exports the moved symbols so ``girder_volview.facade.processing.<name>``
stays a stable import surface.

NOTE for tests (Chunk 32 monkeypatch-target contract): a ``setattr`` on THIS
shim rebinds only the shim's name; it does NOT reach a call site inside a
defining module. Patch/read the DEFINING module (``inputs`` / ``submit`` /
``outputs`` / ``results`` / ``routes``) instead.

Input resolution (Seam 1, client-processing-contract.md) now lives in
``inputs.py``; the slicer_cli_web submit bridge in ``submit.py``; reference-bound
output correlation in ``outputs.py``; status/result projection in ``results.py``;
the REST routes + job creation in ``routes.py``.
"""

# ---------------------------------------------------------------------------
# Provider config (per-launch payload)
# ---------------------------------------------------------------------------

def _providerBaseUrl(folder):
    return f"/api/v1/folder/{folder['_id']}/volview_processing"


# The folder-free root for the job-addressed routes (status/results/cancel),
# which are keyed by job id alone (D5) and mounted on the ``volview_processing``
# resource -- a sibling of ``/folder`` (see routes.py ``_JobResource``). Advertised
# explicitly so the client never string-surgeries the folder segment out of
# ``baseUrl`` (ARCHITECTURE-REVIEW §4.6/§6.4).
_JOBS_BASE_URL = "/api/v1/volview_processing"


def _providerConfigForFolder(folder, user):
    # No advertised sources: the client mints its own input refs from the
    # on-screen volume's provenance (D10 — grouping moved to the client), so the
    # facade advertises only where to reach the provider, not what is loaded.
    #
    # The client zod schema (`src/processing/config.ts` processingProviderConfig)
    # reads only id/label/baseUrl/context. The former `protocol`/`auth` keys were
    # vestigial wire fields the client never read (ARCHITECTURE-REVIEW §4.6) and
    # were removed in Chunk 32 — a `protocol` field is "a standing invitation to
    # switch on it". Absent shapes stay compatible (zod strips unknown keys).
    return {
        "id": "girder-slicer-cli",
        "label": "Analysis",
        "baseUrl": _providerBaseUrl(folder),
        "jobsBaseUrl": _JOBS_BASE_URL,
        "context": {},
    }


def buildProcessingConfigBlock(folder, user):
    return {"providers": [_providerConfigForFolder(folder, user)]}


# ---------------------------------------------------------------------------
# Back-compat re-exports — the moved symbols, so `processing.<name>` still
# resolves. The DEFINING module is authoritative for monkeypatching (see the
# module docstring); these names are references to the same objects.
# ---------------------------------------------------------------------------

from .slicer_spec import translate_slicer_xml, parse_cli  # noqa: E402,F401

from .inputs import (  # noqa: E402,F401
    resolveSourceRefToFolder,
    _stripTypedSourceRef,
    resolveInputUrisToFileIds,
    _PROXIABLE_MARKER,
    _fileIdFromMintedUri,
    _TRANSIENT_META_KEY,
    _TRANSIENT_ORPHAN_TTL,
    _isTransientItem,
    _transientItemIdForFile,
    _collectTransientInputItemIds,
    _markJobTransients,
    _collectInputUris,
    _stampJobContext,
    _removeTransientItems,
    _cleanupTransientOnJobDone,
    _sweepOrphanTransients,
    _streamBodyIntoItem,
    _tagItemTransient,
    _LAUNCH_FOLDER_FIELD,
    _TASK_ID_FIELD,
    _INPUT_URIS_FIELD,
)

from .submit import (  # noqa: E402,F401
    _slicerCliAvailable,
    _listCliItems,
    _findCliItem,
    _cliItemToSummary,
    _DEFAULT_ALLOWED_CATEGORIES,
    _ALLOWED_CATEGORIES_ENV,
    _allowedCategories,
    _taskInScope,
    _scopedCliItems,
    _findScopedCliItem,
    _COMPOUND_EXTENSIONS,
    _splitExt,
    _defaultExtensionForOutput,
    _outputExtension,
    _candidateOutputName,
    _firstInputBaseName,
    _autofillOutputs,
    _rejectReservedSubmitParams,
    _translateValuesToSlicerParams,
)

from .outputs import (  # noqa: E402,F401
    _OUTPUTS_FIELD,
    _OUTPUT_SPECS_FIELD,
    _JOB_TOKEN_FIELD,
    _JOB_TOKENS_FIELD,
    _parseOutputReference,
    _jobForOutputUpload,
    _tagJobOutputItem,
    _recordJobOutput,
    _bindJobOutputs,
    _capturedUploadTokens,
)

from .results import (  # noqa: E402,F401
    _projectJobState,
    _projectJobStatus,
    _projectFinishedAt,
    _projectJobHandle,
    _intentForOutput,
    _recordedJobOutputs,
    _recordedOutputSpecs,
    _loadOutputFile,
    _collectJobResults,
    _jobResultsPayload,
)

from .routes import (  # noqa: E402,F401
    addProcessingRoutes,
    listTasks,
    listRecentJobs,
    getTaskSpec,
    _genDockerJob,
    runTask,
    getJob,
    getJobResults,
    cancelJob,
    stageInput,
    _JobResource,
)
