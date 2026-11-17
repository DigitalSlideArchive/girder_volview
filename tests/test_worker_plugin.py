"""The Girder Worker plugin runs the folder -> job deletion cascade.

Girder 5 runs folder, folder-contents and collection deletes as tasks in the
local Girder Worker, which does not load Girder server plugins. These tests use
no ``server`` fixture, so the VolView server plugin is not loaded, and call
Girder's ``deleteFolderTask`` in-process the way the worker runs it.

Needs a live pytest-girder Mongo; the module self-skips when it is unreachable.
"""

import importlib

from conftest import _folderExists, _jobExists, _makeOwnedJob, mongo_reachable

import pytest
from girder import events

from girder_volview.backend import outputs
from girder_volview.worker_plugin import VolViewWorkerPlugin

pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo; unavailable offline",
)

_HANDLER_NAME = "girder_volview.backend.outputs"
_CASCADE_EVENTS = ("model.job.remove", "model.folder.remove")


@pytest.fixture
def unboundCascades():
    """Start with the cascades unbound, as in a worker without this plugin.

    Earlier tests in the same process may have bound them (a ``server`` fixture
    loading the plugin, or girder_worker instantiating its plugins on import).
    Afterwards leave them bound, which is what either of those produces.
    """
    # The first import of girder_worker.app (via girder.tasks) instantiates
    # every girder_worker plugin, which binds the cascades; do it before
    # unbinding so it cannot rebind them mid-test.
    importlib.import_module("girder.tasks")
    for eventName in _CASCADE_EVENTS:
        events.unbind(eventName, _HANDLER_NAME)
    yield
    outputs.bindDeletionCascades()


def _deleteFolderInWorkerTask(folder, user):
    from girder.tasks import deleteFolderTask

    deleteFolderTask(
        folderId=str(folder["_id"]), progress=False, userId=str(user["_id"])
    )


def test_without_worker_plugin_job_outlives_its_folder(
    owner, ownerFolder, unboundCascades
):
    # The gap the worker plugin closes: no handler, so the job record survives.
    from girder_jobs.constants import JobStatus

    job, outputFolder = _makeOwnedJob(owner, ownerFolder, status=JobStatus.SUCCESS)

    _deleteFolderInWorkerTask(outputFolder, owner)

    assert not _folderExists(outputFolder["_id"])
    assert _jobExists(job["_id"])


def test_worker_plugin_deletes_job_with_its_folder(
    owner, ownerFolder, unboundCascades
):
    # Girder Worker's own entry-point discovery, as at worker startup, so this
    # also checks the setup.py registration.
    from girder_jobs.constants import JobStatus
    from girder_worker.entrypoint import get_plugin_task_modules

    get_plugin_task_modules()
    job, outputFolder = _makeOwnedJob(owner, ownerFolder, status=JobStatus.SUCCESS)

    _deleteFolderInWorkerTask(outputFolder, owner)

    assert not _folderExists(outputFolder["_id"])
    assert not _jobExists(job["_id"])


def test_worker_plugin_refuses_live_job_folder(owner, ownerFolder, unboundCascades):
    from girder.exceptions import RestException
    from girder_jobs.constants import JobStatus

    VolViewWorkerPlugin(app=None)
    job, outputFolder = _makeOwnedJob(owner, ownerFolder, status=JobStatus.RUNNING)

    with pytest.raises(RestException):
        _deleteFolderInWorkerTask(outputFolder, owner)

    assert _jobExists(job["_id"])
