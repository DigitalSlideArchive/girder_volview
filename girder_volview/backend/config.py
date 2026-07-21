"""Processing backend -- the per-launch provider-config block.

The launch manifest injects this block so the client knows where to reach the
processing provider (``baseUrl`` / ``jobsBaseUrl``) for the folder it opened.
Both URLs derive from the runtime ``getApiRoot()`` so a non-default API mount
still resolves.
"""

from girder.utility.server import getApiRoot

# The mount segment of every processing route: the folder-tree routes
# (``/folder/:id/volview_processing/...``) and the folder-free job resource
# (``/volview_processing/...``, see routes.py ``_JobResource``). Shared with the
# route registration so the advertised URLs and the mounted routes cannot drift.
PROCESSING_ROUTE_NAME = "volview_processing"
PROCESSING_PROVIDER_ID_PREFIX = "girder-slicer-cli"


def processingProviderId(folderId):
    """Return the stable provider identity advertised for a launch folder."""
    return "%s:%s" % (PROCESSING_PROVIDER_ID_PREFIX, folderId)


def buildProcessingConfigBlock(folder):
    # The block advertises only where to reach the provider, never what is
    # loaded: the client mints its own input refs from the on-screen volume's
    # provenance. The client zod schema (`src/processing/config.ts`
    # processingProviderConfig) reads only id/label/baseUrl/jobsBaseUrl/context
    # and strips unknown keys, so an added field stays compatible.
    #
    # The provider ID is FOLDER-SCOPED and immutable: it carries the launch
    # folder id so two folders open simultaneously register as two distinct
    # providers (the client keys every job by (providerId, jobId)); a bare
    # "girder-slicer-cli" would make both folders share one mutable identity. The
    # label carries the folder name so the picker distinguishes them (fall back to
    # bare "Analysis" when a folder document has no name).
    #
    # Both URLs are origin-relative and keyed off getApiRoot() -- the SAME mount
    # utils.makeFileDownloadUrl and inputs._fileIdFromMintedUri use. Hardcoding
    # "/api/v1" 404s every submit/status/results/stage call on a non-default API
    # mount (e.g. /girder/api/v1). jobsBaseUrl is the folder-free root for the
    # job-addressed routes (status/results/cancel), advertised explicitly so the
    # client never string-surgeries the folder segment out of baseUrl.
    folderName = folder.get("name")
    return {
        "providers": [
            {
                "id": processingProviderId(folder["_id"]),
                "label": "Analysis — %s" % folderName if folderName else "Analysis",
                "baseUrl": (
                    f"/{getApiRoot()}/folder/{folder['_id']}/{PROCESSING_ROUTE_NAME}"
                ),
                "jobsBaseUrl": f"/{getApiRoot()}/{PROCESSING_ROUTE_NAME}",
                "context": {},
            }
        ]
    }
