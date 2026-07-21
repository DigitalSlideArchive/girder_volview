"""Server-fixture coverage for the reverse cascade (folder delete -> job delete).

Each job's private output folder nests inside the launch folder's single
``volview-jobs`` container, and the deletion cascade is bidirectional:

* removing a job's output folder in the Girder hierarchy removes the job record
  (which sweeps its staged inputs via the job-side cascade);
* removing the whole container recurses per job folder — the ADMIN-gated
  "clear this dataset's job history" gesture;
* a LIVE (non-terminal) job blocks the gesture: the REST route 409s BEFORE any
  contents are cleaned, and the model-level handler refuses too (shell guard for
  direct model callers);
* the job-side cascade (VolView's DELETE) still works — the in-progress marker
  stops the reverse handler from re-entering ``JobModel.remove`` mid-delete.

Needs a live pytest-girder server + Mongo; the module self-skips when the test
Mongo is unreachable.
"""

import json
from conftest import (
    _folderExists,
    _itemExists,
    _jobExists,
    _makeOwnedJob,
    _reload,
    _stageTransientInput,
    makeUser,
    mongo_reachable,
)
import uuid

import pytest

from girder_volview.backend import outputs, routes
from girder_volview.utils import JOB_OUTPUT_FOLDER_META_KEY


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_job_deletion_routes); "
    "unavailable offline",
)


@pytest.fixture
def launchFolder(ownerFolder):
    return ownerFolder


def _container(launchFolder):
    from girder.models.folder import Folder

    return Folder().findOne(
        {
            "parentId": launchFolder["_id"],
            "parentCollection": "folder",
            "name": routes.JOBS_CONTAINER_NAME,
        }
    )


@pytest.mark.plugin("volview")
def test_output_folders_nest_in_one_marked_container(server, owner, launchFolder):
    from girder.constants import AccessType
    from girder.models.folder import Folder

    a = routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)
    b = routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)

    container = _container(launchFolder)
    assert container is not None
    assert str(a["parentId"]) == str(container["_id"])
    assert str(b["parentId"]) == str(container["_id"])
    # The container carries the manifest-exclusion marker.
    assert container["meta"][JOB_OUTPUT_FOLDER_META_KEY] is True
    # Nesting leaves the per-job privacy properties intact: marked, non-public,
    # ACL replaced with a submitter-only ADMIN list.
    for jobFolder in (a, b):
        assert jobFolder["meta"][JOB_OUTPUT_FOLDER_META_KEY] is True
        assert jobFolder["public"] is False
        access = Folder().getFullAccessList(jobFolder)
        assert access["groups"] == []
        assert [(u["id"], u["level"]) for u in access["users"]] == [
            (owner["_id"], AccessType.ADMIN)
        ]


@pytest.mark.plugin("volview")
def test_output_folder_acl_failure_removes_partial_folder(
    server, owner, launchFolder, monkeypatch
):
    from girder.models.folder import Folder

    submissionId = uuid.uuid4().hex

    def failAccessList(*args, **kwargs):
        raise RuntimeError("cannot set output ACL")

    monkeypatch.setattr(Folder, "setAccessList", failAccessList)

    with pytest.raises(RuntimeError, match="cannot set output ACL"):
        routes._createJobOutputFolder(launchFolder, owner, submissionId)

    container = _container(launchFolder)
    assert container is not None
    assert (
        Folder().findOne(
            {
                "parentId": container["_id"],
                "parentCollection": "folder",
                "name": "volview-job-%s" % submissionId,
            }
        )
        is None
    )


@pytest.mark.plugin("volview")
def test_folder_delete_removes_job_and_staged_inputs(server, owner, launchFolder):
    from girder.models.folder import Folder
    from girder_jobs.constants import JobStatus

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.SUCCESS)
    stagedItemId = _stageTransientInput(owner, launchFolder, job)

    Folder().remove(Folder().load(outputFolder["_id"], force=True))

    assert not _folderExists(outputFolder["_id"])
    assert not _jobExists(job["_id"])
    # The reverse cascade routed through JobModel.remove, so the job-side sweep
    # still cleaned the staged input.
    assert not _itemExists(stagedItemId)


@pytest.mark.plugin("volview")
def test_container_delete_clears_all_jobs(server, owner, launchFolder):
    from girder.models.folder import Folder
    from girder_jobs.constants import JobStatus

    jobA, folderA = _makeOwnedJob(owner, launchFolder, status=JobStatus.SUCCESS)
    jobB, folderB = _makeOwnedJob(owner, launchFolder, status=JobStatus.ERROR)
    container = _container(launchFolder)

    Folder().remove(Folder().load(container["_id"], force=True))

    assert not _folderExists(container["_id"])
    assert not _folderExists(folderA["_id"])
    assert not _folderExists(folderB["_id"])
    assert not _jobExists(jobA["_id"])
    assert not _jobExists(jobB["_id"])
    assert _folderExists(launchFolder["_id"])


@pytest.mark.plugin("volview")
def test_live_job_folder_model_remove_is_blocked(server, owner, launchFolder):
    from girder.exceptions import RestException
    from girder.models.folder import Folder
    from girder_jobs.constants import JobStatus

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.RUNNING)

    with pytest.raises(RestException):
        Folder().remove(Folder().load(outputFolder["_id"], force=True))

    assert _folderExists(outputFolder["_id"])
    assert _jobExists(job["_id"])
    # Ownership is intact, so the normal delete works once the job settles.
    assert _reload(job)[outputs._OUTPUT_FOLDER_ID_FIELD] == str(outputFolder["_id"])


def _restDeleteFolder(server, folderId, user):
    return server.request(
        path="/folder/%s" % folderId,
        method="DELETE",
        user=user,
        isJson=False,
        exception=True,
    )


@pytest.mark.plugin("volview")
def test_live_job_folder_rest_delete_409s_before_cleaning(
    server, owner, launchFolder
):
    from girder.models.item import Item
    from girder_jobs.constants import JobStatus

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.RUNNING)
    # A partial output already inside the live job's folder: the REST guard runs
    # before Folder.remove's clean(), so it must survive the refused delete.
    partial = Item().createItem("partial.nrrd", owner, outputFolder)

    for target in (outputFolder["_id"], _container(launchFolder)["_id"]):
        resp = _restDeleteFolder(server, target, owner)
        assert resp.output_status.startswith(b"409")
        assert _folderExists(outputFolder["_id"])
        assert _jobExists(job["_id"])
        assert _itemExists(partial["_id"])

    from girder_jobs.models.job import Job

    Job().updateJob(_reload(job), status=JobStatus.SUCCESS)
    resp = _restDeleteFolder(server, outputFolder["_id"], owner)
    assert resp.output_status.startswith(b"200")
    assert not _folderExists(outputFolder["_id"])
    assert not _jobExists(job["_id"])


@pytest.mark.plugin("volview")
def test_job_delete_still_cascades_folder_without_reentry(
    server, owner, launchFolder
):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.SUCCESS)

    # Direct model removal exercises the same handler chain as the DELETE route.
    Job().remove(_reload(job))

    assert not _jobExists(job["_id"])
    assert not _folderExists(outputFolder["_id"])
    # No leaked in-progress markers.
    assert outputs._CASCADING_FOLDER_IDS == set()


@pytest.mark.plugin("volview")
def test_live_job_blocks_ancestor_folder_rest_delete(server, owner, launchFolder):
    from girder.models.item import Item
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.RUNNING)
    partial = Item().createItem("partial.nrrd", owner, outputFolder)

    # The launch folder carries no marker, yet deleting it would recursively
    # clean the live job's folder -- the preflight must refuse the whole gesture.
    resp = _restDeleteFolder(server, launchFolder["_id"], owner)
    assert resp.output_status.startswith(b"409")
    assert _folderExists(launchFolder["_id"])
    assert _folderExists(outputFolder["_id"])
    assert _itemExists(partial["_id"])

    # Once settled, the ancestor delete fires the reverse cascade per nested
    # job folder.
    Job().updateJob(_reload(job), status=JobStatus.SUCCESS)
    resp = _restDeleteFolder(server, launchFolder["_id"], owner)
    assert resp.output_status.startswith(b"200")
    assert not _folderExists(launchFolder["_id"])
    assert not _jobExists(job["_id"])


@pytest.mark.plugin("volview")
def test_live_job_guard_survives_stripped_marker(server, owner, launchFolder):
    from girder.models.folder import Folder
    from girder_jobs.constants import JobStatus

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.RUNNING)
    folder = Folder().load(outputFolder["_id"], force=True)
    folder.get("meta", {}).pop(JOB_OUTPUT_FOLDER_META_KEY, None)
    Folder().save(folder)

    # Persisted job ownership, not folder metadata, drives the guard.
    resp = _restDeleteFolder(server, outputFolder["_id"], owner)
    assert resp.output_status.startswith(b"409")
    assert _folderExists(outputFolder["_id"])
    assert _jobExists(job["_id"])


@pytest.mark.plugin("volview")
def test_failed_job_remove_restores_folder_pointer(
    server, owner, launchFolder, monkeypatch
):
    from girder.models.folder import Folder
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.SUCCESS)

    def _boom(self, doc):
        raise RuntimeError("simulated job-remove failure")

    monkeypatch.setattr(Job, "remove", _boom)
    with pytest.raises(RuntimeError):
        Folder().remove(Folder().load(outputFolder["_id"], force=True))
    monkeypatch.undo()

    # The folder shell is retained AND the job still points at it, so a retry
    # can re-associate and complete the delete.
    assert _folderExists(outputFolder["_id"])
    assert _jobExists(job["_id"])
    assert _reload(job)[outputs._OUTPUT_FOLDER_ID_FIELD] == str(outputFolder["_id"])

    Folder().remove(Folder().load(outputFolder["_id"], force=True))
    assert not _folderExists(outputFolder["_id"])
    assert not _jobExists(job["_id"])


@pytest.mark.plugin("volview")
def test_unmarked_user_folder_named_volview_jobs_is_not_adopted(
    server, owner, launchFolder
):
    from girder.exceptions import RestException
    from girder.models.folder import Folder
    from girder.models.item import Item

    userFolder = Folder().createFolder(
        launchFolder,
        routes.JOBS_CONTAINER_NAME,
        parentType="folder",
        creator=owner,
        public=False,
    )
    keepsake = Item().createItem("precious.nrrd", owner, userFolder)

    with pytest.raises(RestException) as excinfo:
        routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)
    assert excinfo.value.code == 409

    reloaded = Folder().load(userFolder["_id"], force=True)
    assert not (reloaded.get("meta") or {}).get(JOB_OUTPUT_FOLDER_META_KEY)
    assert _itemExists(keepsake["_id"])


@pytest.mark.plugin("volview")
def test_marked_container_is_reused(server, owner, launchFolder):
    a = routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)
    b = routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)
    assert str(a["parentId"]) == str(b["parentId"])
    container = _container(launchFolder)
    assert container["meta"][JOB_OUTPUT_FOLDER_META_KEY] is True


@pytest.mark.plugin("volview")
def test_container_create_race_does_not_adopt(server, owner, launchFolder, monkeypatch):
    """A user folder that appears BETWEEN the pre-check and the create must not
    be adopted (marker-stamped): the lost creation race re-runs the marker
    check and 409s. Simulated by blinding the first pre-check ``findOne``."""
    from girder.exceptions import RestException
    from girder.models.folder import Folder
    from girder.models.item import Item

    userFolder = Folder().createFolder(
        launchFolder,
        routes.JOBS_CONTAINER_NAME,
        parentType="folder",
        creator=owner,
        public=False,
    )
    keepsake = Item().createItem("precious.nrrd", owner, userFolder)

    realFindOne = Folder.findOne
    blinded = {"done": False}

    def blindFirstContainerLookup(self, query=None, **kwargs):
        if (
            not blinded["done"]
            and isinstance(query, dict)
            and query.get("name") == routes.JOBS_CONTAINER_NAME
        ):
            blinded["done"] = True
            return None
        return realFindOne(self, query, **kwargs)

    monkeypatch.setattr(Folder, "findOne", blindFirstContainerLookup)
    with pytest.raises(RestException) as excinfo:
        routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)
    assert excinfo.value.code == 409
    assert blinded["done"]

    reloaded = Folder().load(userFolder["_id"], force=True)
    assert not (reloaded.get("meta") or {}).get(JOB_OUTPUT_FOLDER_META_KEY)
    assert _itemExists(keepsake["_id"])


@pytest.fixture
def admin(db):
    return makeUser("cascadeadmin", admin=True)


def _collectionLaunchFolder(owner, name="cascade-collection"):
    from girder.models.collection import Collection
    from girder.models.folder import Folder

    collection = Collection().createCollection(name, creator=owner, public=False)
    folder = Folder().createFolder(
        collection, "launch", parentType="collection", creator=owner, public=False
    )
    return collection, folder


@pytest.mark.plugin("volview")
def test_live_job_blocks_collection_rest_delete(server, owner):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    collection, launchFolder = _collectionLaunchFolder(owner)
    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.RUNNING)

    def _deleteCollection():
        return server.request(
            path="/collection/%s" % collection["_id"],
            method="DELETE",
            user=owner,
            isJson=False,
            exception=True,
        )

    resp = _deleteCollection()
    assert resp.output_status.startswith(b"409")
    assert _folderExists(outputFolder["_id"])
    assert _jobExists(job["_id"])

    Job().updateJob(_reload(job), status=JobStatus.SUCCESS)
    resp = _deleteCollection()
    assert resp.output_status.startswith(b"200")
    assert not _folderExists(outputFolder["_id"])
    assert not _jobExists(job["_id"])


@pytest.mark.plugin("volview")
def test_live_job_blocks_user_rest_delete(server, owner, launchFolder, admin):
    from girder.models.user import User
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.RUNNING)

    def _deleteUser():
        return server.request(
            path="/user/%s" % owner["_id"],
            method="DELETE",
            user=admin,
            isJson=False,
            exception=True,
        )

    resp = _deleteUser()
    assert resp.output_status.startswith(b"409")
    assert _folderExists(outputFolder["_id"])
    assert _jobExists(job["_id"])

    Job().updateJob(_reload(job), status=JobStatus.SUCCESS)
    resp = _deleteUser()
    assert resp.output_status.startswith(b"200")
    assert User().load(owner["_id"], force=True, exc=False) is None
    assert not _folderExists(outputFolder["_id"])


@pytest.mark.plugin("volview")
def test_live_job_blocks_resource_rest_delete(server, owner, launchFolder):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    job, outputFolder = _makeOwnedJob(owner, launchFolder, status=JobStatus.RUNNING)

    def _deleteResources(payload):
        return server.request(
            path="/resource",
            method="DELETE",
            user=owner,
            params={"resources": json.dumps(payload)},
            isJson=False,
            exception=True,
        )

    # Folder ids resolve through the subtree walk...
    resp = _deleteResources({"folder": [str(launchFolder["_id"])]})
    assert resp.output_status.startswith(b"409")
    assert _folderExists(outputFolder["_id"])
    assert _jobExists(job["_id"])

    # ...and collection ids through the base-parent check.
    collection, colLaunch = _collectionLaunchFolder(owner, name="cascade-batch")
    colJob, colOutput = _makeOwnedJob(owner, colLaunch, status=JobStatus.RUNNING)
    resp = _deleteResources({"collection": [str(collection["_id"])]})
    assert resp.output_status.startswith(b"409")
    assert _folderExists(colOutput["_id"])
    assert _jobExists(colJob["_id"])

    for liveJob in (job, colJob):
        Job().updateJob(_reload(liveJob), status=JobStatus.SUCCESS)
    resp = _deleteResources(
        {
            "folder": [str(launchFolder["_id"])],
            "collection": [str(collection["_id"])],
        }
    )
    assert resp.output_status.startswith(b"200")
    assert not _folderExists(outputFolder["_id"])
    assert not _folderExists(colOutput["_id"])
    assert not _jobExists(job["_id"])
    assert not _jobExists(colJob["_id"])
