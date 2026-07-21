"""Shared test scaffolding for the Mongo-backed route suites."""

import io
import socket
import uuid

import pytest

# pytest_girder's own default for --mongo-uri. A resolved uri that differs from
# it means someone configured Mongo deliberately, which changes an unreachable
# probe from "developer is offline" into a misconfiguration worth failing on.
_DEFAULT_MONGO_URI = "mongodb://localhost:27017"

_mongoUri = _DEFAULT_MONGO_URI


def pytest_configure(config):
    # Captured at configure time, which precedes collection, so the module-level
    # ``pytestmark`` skipif in each route suite sees the real uri.
    global _mongoUri
    _mongoUri = config.getoption("--mongo-uri", default=_DEFAULT_MONGO_URI)


def mongo_reachable(timeout=0.5):
    """Whether the Mongo the ``db`` fixture will use is reachable.

    Probes the host/port from ``--mongo-uri`` -- the same option
    ``pytest_girder``'s ``db`` fixture connects with, and the only thing that
    actually selects a database -- so the Mongo-backed route suites skip cleanly
    offline instead of erroring.

    Raises when an explicitly configured Mongo is unreachable: silently skipping
    there once left CI green while every route suite was being skipped.
    """
    host, port = "localhost", 27017
    uri = _mongoUri or ""
    if uri.startswith("mongodb://"):
        netloc = uri[len("mongodb://") :].split("/", 1)[0].split(",", 1)[0]
        if ":" in netloc:
            host, port_str = netloc.rsplit(":", 1)
            port = int(port_str) if port_str.isdigit() else port
        elif netloc:
            host = netloc
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as err:
        if uri != _DEFAULT_MONGO_URI:
            raise RuntimeError(
                f"--mongo-uri is {uri} but nothing is listening on {host}:{port}. "
                "Refusing to skip the Mongo-backed route suites: an explicitly "
                "configured Mongo that is unreachable is a broken run, not an "
                "offline one."
            ) from err
        return False


def makeUser(login, admin=False):
    """Create a user whose login is uniquified from ``login``.

    Unique per-test logins: under serial full-suite runs the girder db fixture
    leaks state across tests (a fixed login "already exists" even though the
    fixture reports a fresh database). Unique identities sidestep the leak
    instead of depending on cleanup ordering.
    """
    from girder.models.user import User

    unique = f"{login}{uuid.uuid4().hex[:8]}"
    return User().createUser(
        login=unique,
        password="password123",
        firstName="A",
        lastName="B",
        email=f"{unique}@example.com",
        admin=admin,
    )


@pytest.fixture
def owner(db):
    return makeUser("owner")


@pytest.fixture
def stranger(db):
    return makeUser("stranger")


@pytest.fixture
def ownerFolder(fsAssetstore, owner):
    from girder.models.folder import Folder

    return Folder().createFolder(
        owner, "launch", parentType="user", creator=owner, public=False
    )


API_ROOT = "api/v1"


@pytest.fixture
def _fixed_api_root(monkeypatch):
    # Deterministic mount so the corpus exemplars' ``/api/v1/...`` handles
    # compare byte-for-byte regardless of ambient server config. NOT autouse:
    # modules that need the pin request it explicitly.
    from girder_volview import handles

    monkeypatch.setattr(handles, "getApiRoot", lambda: API_ROOT)


def _reload(job):
    """Reload a job by document or bare id."""
    from girder_jobs.models.job import Job

    jobId = job["_id"] if isinstance(job, dict) else job
    return Job().load(jobId, force=True)


def _drive(job, status):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    paths = {
        JobStatus.QUEUED: [JobStatus.QUEUED],
        JobStatus.RUNNING: [JobStatus.QUEUED, JobStatus.RUNNING],
        JobStatus.SUCCESS: [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCESS],
        JobStatus.ERROR: [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.ERROR],
    }
    for s in paths.get(status, []):
        job = Job().updateJob(_reload(job), status=s)
    return _reload(job)


def _makeOwnedJob(owner, launchFolder, status=None, public=False):
    """A job owning a REAL private output folder (created exactly as runTask does)."""
    from girder_jobs.models.job import Job

    from girder_volview.backend import inputs, outputs, routes

    outputFolder = routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)
    job = Job().createJob(
        title="t",
        type="volview_test",
        user=owner,
        public=public,
        otherFields={
            outputs._OUTPUT_FOLDER_ID_FIELD: str(outputFolder["_id"]),
            inputs._LAUNCH_FOLDER_FIELD: str(launchFolder["_id"]),
            outputs._OUTPUTS_FIELD: {},
        },
    )
    if status is not None:
        job = _drive(job, status)
    return _reload(job), outputFolder


def _stageTransientInput(owner, launchFolder, job):
    """Stage a transient input item and record it on the (already terminal) job.

    Stamped AFTER the job is terminal so the terminal-state transient cleanup did
    not already remove it -- the DELETE cascade is then the unambiguous remover."""
    from girder.models.item import Item
    from girder.models.upload import Upload
    from girder_jobs.models.job import Job

    from girder_volview.utils import TRANSIENT_STAGED_META_KEY

    fileDoc = Upload().uploadFromFile(
        io.BytesIO(b"seg-bytes"),
        size=9,
        name="staged.seg.nrrd",
        parentType="folder",
        parent=launchFolder,
        user=owner,
    )
    itemId = fileDoc["itemId"]
    Item().setMetadata(
        Item().load(itemId, force=True), {TRANSIENT_STAGED_META_KEY: True}
    )
    Job().collection.update_one(
        {"_id": job["_id"]},
        {"$set": {TRANSIENT_STAGED_META_KEY: [str(itemId)]}},
    )
    return itemId


def _folderExists(folderId):
    from girder.models.folder import Folder

    return Folder().load(folderId, force=True, exc=False) is not None


def _jobExists(jobId):
    from girder_jobs.models.job import Job

    return Job().load(jobId, force=True, exc=False) is not None


def _itemExists(itemId):
    from girder.models.item import Item

    return Item().load(itemId, force=True, exc=False) is not None


def _uploadFile(folder, user, name, data=b"pixels", meta=None):
    """Upload one file into ``folder``; return (item, file)."""
    from girder.models.item import Item
    from girder.models.upload import Upload

    fileDoc = Upload().uploadFromFile(
        io.BytesIO(data),
        size=len(data),
        name=name,
        parentType="folder",
        parent=folder,
        user=user,
    )
    item = Item().load(fileDoc["itemId"], force=True)
    if meta:
        item = Item().setMetadata(item, meta)
    return item, fileDoc


def _folderManifest(server, folder, user, params=None, **kwargs):
    return server.request(
        path="/folder/%s/volview" % folder["_id"],
        method="GET",
        user=user,
        params=params or {},
        isJson=True,
        **kwargs,
    )


def _itemManifest(server, item, user, **kwargs):
    return server.request(
        path="/item/%s/volview" % item["_id"],
        method="GET",
        user=user,
        isJson=True,
        **kwargs,
    )


class _Event:
    def __init__(self, info):
        self.info = info
