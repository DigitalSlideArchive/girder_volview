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
def test_first_write_collaborator_does_not_gain_container_admin(
    server, owner, launchFolder
):
    from girder.constants import AccessType
    from girder.models.folder import Folder

    collaborator = makeUser("writecollaborator")
    Folder().setUserAccess(
        launchFolder, collaborator, level=AccessType.WRITE, save=True
    )

    routes._createJobOutputFolder(
        launchFolder, collaborator, uuid.uuid4().hex
    )

    container = _container(launchFolder)
    launchAccess = Folder().getFullAccessList(launchFolder)
    containerAccess = Folder().getFullAccessList(container)
    assert sorted((u["id"], u["level"]) for u in containerAccess["users"]) == sorted(
        (u["id"], u["level"]) for u in launchAccess["users"]
    )
    assert containerAccess["groups"] == launchAccess["groups"]
    assert next(
        u["level"]
        for u in containerAccess["users"]
        if u["id"] == collaborator["_id"]
    ) == AccessType.WRITE

    resp = _restDeleteFolder(server, container["_id"], collaborator)
    assert resp.output_status.startswith(b"403")
    assert _folderExists(container["_id"])


@pytest.mark.plugin("volview")
def test_output_folder_acl_failure_removes_partial_folder(
    server, owner, launchFolder, monkeypatch
):
    from girder.models.folder import Folder

    submissionId = uuid.uuid4().hex

    realSetAccessList = Folder.setAccessList
    calls = {"count": 0}

    def failOutputAccessList(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("cannot set output ACL")
        return realSetAccessList(self, *args, **kwargs)

    monkeypatch.setattr(Folder, "setAccessList", failOutputAccessList)

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
    """A genuinely unmarked folder that lands in the window between the
    election's (in-memory, DB-free) fast-path check and its own
    ``createFolder`` call must not be adopted: the resulting
    ``ValidationException`` still refuses (409) rather than mistaking an
    unmarked collision for a race-mate. Simulated by sneaking a real,
    unmarked, same-named user folder in immediately before the election's own
    ``createFolder`` call actually runs -- the analogous race for the new
    atomic-election design, in which there is no longer a separate DB lookup
    upstream of ``createFolder`` to blind."""
    from girder.exceptions import RestException
    from girder.models.folder import Folder
    from girder.models.item import Item

    realCreateFolder = Folder.createFolder
    snuck = {"itemId": None}

    def sneakInAUserFolder(self, parent, name, **kwargs):
        if snuck["itemId"] is None and name == routes.JOBS_CONTAINER_NAME:
            userFolder = realCreateFolder(
                self, parent, name, parentType="folder", creator=owner, public=False
            )
            snuck["itemId"] = Item().createItem("precious.nrrd", owner, userFolder)[
                "_id"
            ]
        return realCreateFolder(self, parent, name, **kwargs)

    monkeypatch.setattr(Folder, "createFolder", sneakInAUserFolder)

    with pytest.raises(RestException) as excinfo:
        routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)
    assert excinfo.value.code == 409
    assert snuck["itemId"] is not None

    userFolder = Folder().findOne(
        {
            "parentId": launchFolder["_id"],
            "parentCollection": "folder",
            "name": routes.JOBS_CONTAINER_NAME,
        }
    )
    assert userFolder is not None
    assert not (userFolder.get("meta") or {}).get(JOB_OUTPUT_FOLDER_META_KEY)
    assert _itemExists(snuck["itemId"])


@pytest.mark.plugin("volview")
def test_marked_orphan_container_with_no_recorded_pointer_is_adopted(
    server, owner, launchFolder
):
    """A crash between creating+marking a container and recording it on the
    launch folder leaves a real, marked, but unrecorded orphan. The next
    election must adopt it (record it) rather than 409 -- an unmarked
    collision refuses, but a MARKED one is always ours to reclaim."""
    from girder.models.folder import Folder

    orphan = Folder().createFolder(
        launchFolder,
        routes.JOBS_CONTAINER_NAME,
        parentType="folder",
        creator=owner,
        public=False,
    )
    orphan = Folder().setMetadata(orphan, {JOB_OUTPUT_FOLDER_META_KEY: True})
    reloaded = Folder().load(launchFolder["_id"], force=True)
    assert not (reloaded.get("meta") or {}).get(routes._JOBS_CONTAINER_ID_META_KEY)

    adopted = routes._jobsContainerFolder(reloaded, owner)

    assert str(adopted["_id"]) == str(orphan["_id"])
    stillOnlyOne = list(
        Folder().find(
            {
                "parentId": launchFolder["_id"],
                "parentCollection": "folder",
                "name": routes.JOBS_CONTAINER_NAME,
            }
        )
    )
    assert len(stillOnlyOne) == 1
    reloaded = Folder().load(launchFolder["_id"], force=True)
    assert reloaded["meta"][routes._JOBS_CONTAINER_ID_META_KEY] == str(orphan["_id"])


@pytest.mark.plugin("volview")
def test_creator_keeps_candidate_adopted_by_a_race_mate(
    server, owner, launchFolder, monkeypatch
):
    """A race-mate that loses ``createFolder`` can find the creator's candidate
    already MARKED and record it before the creator's own claim runs -- a win
    by proxy. The creator's failed claim must then RETURN its candidate, never
    delete it: the race-mate has already returned that exact folder to its own
    caller. Simulated deterministically by running a full adopting election
    inside the window between the creator's marker stamp and its claim."""
    from girder.models.folder import Folder

    realSetMetadata = Folder.setMetadata
    adopted = {}

    def adoptBeforeCreatorClaims(self, folder, metadata, **kwargs):
        marked = realSetMetadata(self, folder, metadata, **kwargs)
        if metadata.get(JOB_OUTPUT_FOLDER_META_KEY) and not adopted:
            adopted["container"] = routes._jobsContainerFolder(
                Folder().load(launchFolder["_id"], force=True), owner
            )
        return marked

    monkeypatch.setattr(Folder, "setMetadata", adoptBeforeCreatorClaims)

    creatorContainer = routes._jobsContainerFolder(launchFolder, owner)

    assert adopted, "the adopting election must run inside the mark->claim window"
    assert str(creatorContainer["_id"]) == str(adopted["container"]["_id"])
    assert Folder().load(creatorContainer["_id"], force=True) is not None
    containers = list(
        Folder().find(
            {
                "parentId": launchFolder["_id"],
                "parentCollection": "folder",
                "name": routes.JOBS_CONTAINER_NAME,
            }
        )
    )
    assert len(containers) == 1
    reloaded = Folder().load(launchFolder["_id"], force=True)
    assert reloaded["meta"][routes._JOBS_CONTAINER_ID_META_KEY] == str(
        creatorContainer["_id"]
    )


@pytest.mark.plugin("volview")
def test_stale_recorded_pointer_is_healed(server, owner, launchFolder):
    """If the recorded container is later removed out from under the launch
    folder's pointer (the record goes stale), the next election heals the
    pointer and creates a fresh container rather than erroring forever."""
    from girder.models.folder import Folder

    first = routes._jobsContainerFolder(launchFolder, owner)
    launchFolder = Folder().load(launchFolder["_id"], force=True)
    assert launchFolder["meta"][routes._JOBS_CONTAINER_ID_META_KEY] == str(
        first["_id"]
    )

    Folder().remove(first)

    second = routes._jobsContainerFolder(launchFolder, owner)
    assert str(second["_id"]) != str(first["_id"])
    reloaded = Folder().load(launchFolder["_id"], force=True)
    assert reloaded["meta"][routes._JOBS_CONTAINER_ID_META_KEY] == str(second["_id"])


@pytest.mark.plugin("volview")
def test_jobsContainerFolder_concurrent_calls_converge_on_one_container(server, owner):
    """Concurrent callers of the create-or-adopt election must converge on
    exactly one container: plural labelmap staging fans N concurrent ``/stage``
    calls into ONE submission's jobs container, so every call reaches
    ``_jobsContainerFolder`` at nearly the same instant.

    Exercises the helper directly rather than the full stage handler: it takes
    plain Folder/User documents and talks to Mongo directly (thread-safe
    pymongo), so it is the precise unit that owns the race -- the REST layer
    around it (current user, multipart parsing) is orthogonal. A ``Barrier``
    holds every thread at the starting line so the create/record window is
    maximally contended. Each round needs a FRESH launch folder, since a
    container recorded by a prior round would let later rounds win on the
    read-only fast path without exercising the create/record race at all;
    several rounds run because a single election can converge by luck.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from girder.models.folder import Folder

    concurrency = 8
    rounds = 3

    for roundIndex in range(rounds):
        roundFolder = Folder().createFolder(
            owner,
            "launch-race-%d" % roundIndex,
            parentType="user",
            creator=owner,
            public=False,
        )
        start = threading.Barrier(concurrency)

        def race(_i, folder=roundFolder, barrier=start):
            barrier.wait()
            return routes._jobsContainerFolder(folder, owner)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(race, range(concurrency)))

        ids = {str(result["_id"]) for result in results}
        assert len(ids) == 1, "every concurrent call must return the SAME container"

        containers = list(
            Folder().find(
                {
                    "parentId": roundFolder["_id"],
                    "parentCollection": "folder",
                    "name": routes.JOBS_CONTAINER_NAME,
                }
            )
        )
        assert len(containers) == 1
        assert str(containers[0]["_id"]) == ids.pop()


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
