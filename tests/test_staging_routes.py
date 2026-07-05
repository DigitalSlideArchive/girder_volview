"""Server-fixture coverage for the Chunk 14 staging endpoint + transient cleanup.

What the offline ``test_transient_cleanup`` unit tests cannot show, exercised
here against real Girder models + the live cherrypy pipeline:

1. *Stage -> resolvable URI* -- a raw-bytes POST to the type-agnostic staging
   route returns a facade-minted ``{uris}`` that resolves through the SAME
   own-scheme path as any other input, and the created item is tagged transient.
2. *Terminal cleanup* -- a staged input bound to a real job is deleted once that
   job reaches a terminal state, via the real ``jobs.job.update.after`` handler.
3. *Orphan sweep* -- a real Mongo age query: a staged item older than the TTL is
   swept on the next staging call; a younger one is left alone.
4. *CSRF on the route* -- a cross-site / no-signal POST is rejected 403.

Like ``test_csrf_routes`` this needs a live pytest-girder server + Mongo; the
module self-skips when the test Mongo is unreachable so the offline gate stays
green, and runs (and must pass) wherever Mongo is present.
"""

import datetime
import json
import os
import socket
import types

import pytest

from girder_volview.facade import processing, routes, submit


# ---------------------------------------------------------------------------
# Self-skip when no live test Mongo is reachable (mirrors test_csrf_routes)
# ---------------------------------------------------------------------------

def _mongo_reachable(timeout=0.5):
    host, port = "localhost", 27017
    uri = os.environ.get("GIRDER_TEST_DB", "")
    if uri.startswith("mongodb://"):
        netloc = uri[len("mongodb://"):].split("/", 1)[0].split(",", 1)[0]
        if ":" in netloc:
            host, port_str = netloc.rsplit(":", 1)
            port = int(port_str) if port_str.isdigit() else port
        elif netloc:
            host = netloc
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_csrf_routes); unavailable offline",
)


_CLI_XML = (
    '<?xml version="1.0"?>'
    "<executable><category>Segmentation</category><title>Seg</title>"
    "<parameters>"
    '<image type="label"><name>inputVolume</name><channel>input</channel></image>'
    "</parameters></executable>"
)

STAGE_PATH = "/folder/%s/volview_processing/stage"
RUN_PATH = "/folder/%s/volview_processing/tasks/sometask/run"


# ---------------------------------------------------------------------------
# Real users / folders
# ---------------------------------------------------------------------------

@pytest.fixture
def owner(db):
    from girder.models.user import User
    return User().createUser(
        login="stageowner", password="password123", firstName="A", lastName="B",
        email="stageowner@example.com", admin=False,
    )


@pytest.fixture
def ownerFolder(fsAssetstore, owner):
    from girder.models.folder import Folder
    return Folder().createFolder(
        owner, "launch", parentType="user", creator=owner, public=False
    )


@pytest.fixture
def realJobStub(monkeypatch):
    """Stub the slicer_cli_web touch points so runTask reaches transient marking
    without docker, but create a REAL Girder job so the terminal transition fires
    the real jobs.job.update.after handler."""
    cli = types.SimpleNamespace(name="Seg", xml=_CLI_XML)
    monkeypatch.setattr(submit, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(submit, "_findScopedCliItem", lambda taskId, user: cli)

    def fake_gen(cliItem, params, user):
        from girder_jobs.models.job import Job
        return Job().createJob(
            title="stub", type="volview_test", user=user, public=False
        )

    monkeypatch.setattr(routes, "_genDockerJob", fake_gen)
    return cli


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def _stage(server, folder, user, content, name="staged.bin", headers=(("Sec-Fetch-Site", "same-origin"),), isJson=True):
    return server.request(
        path=STAGE_PATH % folder["_id"],
        method="POST",
        user=user,
        params={"name": name},
        body=content,
        type="application/octet-stream",
        additionalHeaders=list(headers),
        isJson=isJson,
        exception=True,
    )


def _run(server, folder, user, values):
    return server.request(
        path=RUN_PATH % folder["_id"],
        method="POST",
        user=user,
        body=json.dumps({"values": values}),
        type="application/json",
        additionalHeaders=[("Sec-Fetch-Site", "same-origin")],
        isJson=True,
        exception=True,
    )


def _itemForUri(uri):
    from girder.models.file import File
    from girder.models.item import Item
    fileId = processing._fileIdFromMintedUri(uri)
    fileDoc = File().load(fileId, force=True)
    return Item().load(fileDoc["itemId"], force=True)


# ---------------------------------------------------------------------------
# 1. Stage -> a URI that resolves like any other input
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_stage_returns_minted_uri_that_resolves(server, owner, ownerFolder):
    resp = _stage(server, ownerFolder, owner, b"seg-bytes", name="seg.seg.nrrd")

    assert resp.output_status.startswith(b"200")
    uris = resp.json["uris"]
    assert isinstance(uris, list) and len(uris) == 1
    # The facade minted an origin-relative proxiable URI (never the client).
    assert uris[0].startswith("/api/v1/file/")

    # It resolves through the SAME own-scheme path as any minted input; the CLI
    # param is the recovered file id.
    fileId = processing._fileIdFromMintedUri(uris[0])
    assert fileId is not None
    params = processing._translateValuesToSlicerParams(
        {"inputVolume": {"type": "labelmap", "uris": uris}},
        user=owner, folder=ownerFolder,
    )
    assert params["inputVolume"] == fileId

    # The staged item is tagged transient (invisible to source listings / history).
    assert _itemForUri(uris[0])["meta"]["volviewTransient"] is True


@pytest.mark.plugin("volview")
def test_stage_is_type_agnostic_about_bytes(server, owner, ownerFolder):
    # Arbitrary, non-seg.nrrd bytes stage identically -- the endpoint never sniffs
    # content (deferred item 12: no extension allow-list, no size cap in v1).
    resp = _stage(server, ownerFolder, owner, b"\x00\x01not a labelmap", name="blob.dat")
    assert resp.output_status.startswith(b"200")
    assert processing._fileIdFromMintedUri(resp.json["uris"][0]) is not None


# ---------------------------------------------------------------------------
# 2. Staged input deleted when its job reaches a terminal state
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_staged_input_deleted_at_job_terminal(server, owner, ownerFolder, realJobStub):
    from girder.models.item import Item
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    uri = _stage(server, ownerFolder, owner, b"labelmap", name="seg.seg.nrrd").json["uris"][0]
    stagedItem = _itemForUri(uri)
    assert stagedItem["meta"]["volviewTransient"] is True

    resp = _run(server, ownerFolder, owner, {"inputVolume": {"type": "labelmap", "uris": [uri]}})
    assert resp.output_status.startswith(b"200")

    job = Job().load(resp.json["jobId"], force=True)
    # The staged input's item was recorded on the job for cleanup.
    assert str(stagedItem["_id"]) in job.get("volviewTransient", [])
    # Non-terminal: the input is still there.
    assert Item().load(stagedItem["_id"], force=True) is not None

    # Legal transition chain to a terminal state (INACTIVE->QUEUED->RUNNING->SUCCESS).
    Job().updateJob(job, status=JobStatus.QUEUED)
    Job().updateJob(job, status=JobStatus.RUNNING)
    Job().updateJob(job, status=JobStatus.SUCCESS)

    # The real jobs.job.update.after handler deleted the transient input at terminal.
    assert Item().load(stagedItem["_id"], force=True) is None


@pytest.mark.plugin("volview")
def test_non_transient_input_survives_job_terminal(server, owner, ownerFolder, realJobStub):
    # A regular (non-staged) input is never recorded and never deleted at job end.
    import io

    from girder.models.item import Item
    from girder.models.upload import Upload
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    from girder_volview.utils import makeFileDownloadUrl as mint

    durable = Upload().uploadFromFile(
        io.BytesIO(b"pixels"), size=6, name="scan.nrrd",
        parentType="folder", parent=ownerFolder, user=owner,
    )
    durableItemId = durable["itemId"]
    value = {"type": "image", "uris": [mint(durable)]}
    resp = _run(server, ownerFolder, owner, {"inputVolume": value})
    job = Job().load(resp.json["jobId"], force=True)
    assert not job.get("volviewTransient")

    Job().updateJob(job, status=JobStatus.QUEUED)
    Job().updateJob(job, status=JobStatus.RUNNING)
    Job().updateJob(job, status=JobStatus.SUCCESS)

    assert Item().load(durableItemId, force=True) is not None


# ---------------------------------------------------------------------------
# 3. Orphan sweep — older-than-TTL swept, younger untouched (real Mongo query)
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_orphan_older_than_ttl_swept_on_next_stage(server, owner, ownerFolder):
    from girder.models.item import Item

    oldUri = _stage(server, ownerFolder, owner, b"old", name="old.bin").json["uris"][0]
    youngUri = _stage(server, ownerFolder, owner, b"young", name="young.bin").json["uris"][0]
    oldItem = _itemForUri(oldUri)
    youngItem = _itemForUri(youngUri)

    # Backdate the old item beyond the TTL (the marker carries no timestamp, so
    # the sweep keys off item['created']).
    stale = (
        datetime.datetime.utcnow()
        - processing._TRANSIENT_ORPHAN_TTL
        - datetime.timedelta(hours=1)
    )
    Item().collection.update_one({"_id": oldItem["_id"]}, {"$set": {"created": stale}})

    # A third staging call sweeps at the top of the handler.
    newUri = _stage(server, ownerFolder, owner, b"new", name="new.bin").json["uris"][0]
    newItem = _itemForUri(newUri)

    assert Item().load(oldItem["_id"], force=True) is None         # swept
    assert Item().load(youngItem["_id"], force=True) is not None   # within TTL
    assert Item().load(newItem["_id"], force=True) is not None     # just staged


# ---------------------------------------------------------------------------
# 4. CSRF is enforced on the staging route (behavior on the wire)
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_stage_route_rejects_cross_site(server, owner, ownerFolder):
    resp = _stage(
        server, ownerFolder, owner, b"bytes", name="x.bin",
        headers=(("Sec-Fetch-Site", "cross-site"),), isJson=False,
    )
    assert resp.output_status.startswith(b"403")


@pytest.mark.plugin("volview")
def test_stage_route_fails_closed_without_browser_signal(server, owner, ownerFolder):
    resp = _stage(server, ownerFolder, owner, b"bytes", name="x.bin", headers=(), isJson=False)
    assert resp.output_status.startswith(b"403")
