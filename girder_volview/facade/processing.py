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
    _readLabelsSidecar,
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


# ---------------------------------------------------------------------------
# Legacy duplicate XML walks — retained here after the code motion so their
# removal lands as an isolated deletion (they are dead: the split modules call
# slicer_spec.parse_cli instead). Deleted next commit.
# ---------------------------------------------------------------------------

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
