"""Girder Worker plugin: VolView's event handlers for the local worker.

Girder 5 runs folder, folder-contents and collection deletes as tasks on the
local Girder Worker queue. That process does not load Girder server plugins, so
without this the job <-> output-folder deletion cascades would not run there
and deleting a job's output folder would leave its job record behind.

Girder Worker instantiates every ``girder_worker_plugins`` entry point when
``girder_worker.app`` is imported, in the web server as well as in the worker.
Rebinding the same handler names in the web server is harmless.
"""

from girder_worker import GirderWorkerPluginABC


class VolViewWorkerPlugin(GirderWorkerPluginABC):
    def __init__(self, app, *args, **kwargs):
        # Imported here, not at module level: this runs while Girder itself is
        # importing girder_worker.app, so keep the plugin import cheap until now.
        from .backend.outputs import bindDeletionCascades

        bindDeletionCascades()
