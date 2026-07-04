"""Server-fixture coverage for Chunk 20 item-launch parity (PLAN rework item 4,
contract Seam 3), against real Girder models + the live cherrypy pipeline.

The pin (client-processing-contract.md "Item-launch parity in v1"): the Jobs tab
and the full job flow work from single-item launches; the *facade* derives the
launch context from the item's PARENT folder, and the client is unaffected
(input URIs come from provenance regardless of launch shape; job status/results/
cancel are job-addressed since Chunk 18). The three launch-context-scoped routes
-- ``listTasks`` / ``runTask`` / ``stageInput`` -- stay folder-scoped and, for an
item launch, must operate on the item's parent folder.

There is NO item-scoped processing route and NO client change: an item-launched
client only ever reads the processing provider ``baseUrl`` it was served, which
the facade points at the item's parent folder via the manifest -> config
indirection (``downloadManifest`` -> ``filesToManifest(files, item['folderId'])``
-> ``/folder/{parentId}/volview_config/...`` -> ``buildProcessingConfigBlock`` ->
``_providerBaseUrl(parentFolder)``). So every test below *derives* the launch
folder from the served config exactly as the client would, then drives the three
routes at that derived folder and asserts parity with the parent folder.

Proven here (needs a live pytest-girder Mongo; self-skips when unreachable so the
offline gate stays green, and must pass wherever Mongo is present):

1. *Derivation* -- an item launch's served processing provider ``baseUrl`` is
   scoped to the item's parent folder (the whole chunk's load-bearing fact).
2. *listTasks* under an item launch reaches the parent folder and returns the
   catalog; the parent-folder READ ACL still gates a stranger.
3. *runTask* under an item launch stamps the job's launch context as the parent
   folder (Chunk 19 coherence) and defaults output back to that same folder.
4. *stageInput* under an item launch lands the transient item in the parent
   folder.
"""

import json
import os
import socket
import types

import pytest

from girder_volview.facade import processing


# ---------------------------------------------------------------------------
# Self-skip when no live test Mongo is reachable (mirrors the other route tests)
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
    reason="needs a live pytest-girder Mongo (like test_staging_routes); "
    "unavailable offline",
)


# A minimal CLI with one output param so runTask's translate step sets the
# default output folder ({param}_folder) — the "default output folder" sub-scope.
_CLI_XML = (
    '<?xml version="1.0"?>'
    "<executable><category>Segmentation</category><title>Seg</title>"
    "<parameters>"
    '<image type="scalar"><name>inputVolume</name><channel>input</channel></image>'
    '<image type="label"><name>outputVolume</name><channel>output</channel></image>'
    "</parameters></executable>"
)

ITEM_MANIFEST_PATH = "/item/%s/volview"
CONFIG_PATH = "/folder/%s/volview_config/.volview_config.yaml"
TASKS_PATH = "/folder/%s/volview_processing/tasks"
STAGE_PATH = "/folder/%s/volview_processing/stage"
RUN_PATH = "/folder/%s/volview_processing/tasks/sometask/run"


# ---------------------------------------------------------------------------
# Users / a parent folder + a single item launched from inside it
# ---------------------------------------------------------------------------

@pytest.fixture
def owner(db):
    from girder.models.user import User
    return User().createUser(
        login="parityowner", password="password123", firstName="A", lastName="B",
        email="parityowner@example.com", admin=False,
    )


@pytest.fixture
def stranger(db):
    from girder.models.user import User
    return User().createUser(
        login="paritystranger", password="password123", firstName="N", lastName="A",
        email="paritystranger@example.com", admin=False,
    )


@pytest.fixture
def parentFolder(fsAssetstore, owner):
    from girder.models.folder import Folder
    # Private so the ACL gate is meaningful for the stranger case.
    return Folder().createFolder(
        owner, "study", parentType="user", creator=owner, public=False
    )


@pytest.fixture
def launchItem(parentFolder, owner):
    """A single image item living inside parentFolder — the item-launch root."""
    import io

    from girder.models.item import Item
    from girder.models.upload import Upload
    file_doc = Upload().uploadFromFile(
        io.BytesIO(b"pixels"), size=6, name="brain.nrrd",
        parentType="folder", parent=parentFolder, user=owner,
    )
    return Item().load(file_doc["itemId"], force=True)


@pytest.fixture
def runStub(monkeypatch):
    """Stub slicer_cli_web so runTask reaches job creation without docker, and
    CAPTURE the translated params so the default-output-folder can be asserted.

    A REAL Girder job is created so ``_stampJobContext`` writes a real launch
    stamp we can read back.
    """
    cli = types.SimpleNamespace(name="Seg", xml=_CLI_XML)
    captured = {}
    monkeypatch.setattr(processing, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(processing, "_findScopedCliItem", lambda taskId, user: cli)

    def fake_gen(cliItem, params, user):
        from girder_jobs.models.job import Job
        captured["params"] = params
        return Job().createJob(
            title="stub", type="volview_test", user=user, public=False
        )

    monkeypatch.setattr(processing, "_genDockerJob", fake_gen)
    return captured


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def _get(server, path, user):
    return server.request(
        path=path, method="GET", user=user, isJson=True, exception=True,
    )


def _segment_after(url, key):
    parts = url.strip("/").split("/")
    return parts[parts.index(key) + 1]


def _served_launch_folder_id(server, item, user):
    """Reproduce, exactly as the client does, the item -> parent-folder launch
    derivation: read the item manifest, follow its config.json to the config
    route, and return the folder id the served processing provider is scoped to.
    """
    manifest = _get(server, ITEM_MANIFEST_PATH % item["_id"], user)
    config_url = next(
        r["url"] for r in manifest.json["resources"] if r["name"] == "config.json"
    )
    config_folder_id = _segment_after(config_url, "folder")
    config = _get(server, CONFIG_PATH % config_folder_id, user)
    provider = config.json["processing"]["providers"][0]
    # The baseUrl is the only processing handle the client is ever given.
    return _segment_after(provider["baseUrl"], "folder")


# ---------------------------------------------------------------------------
# 1. Derivation — the served processing provider is scoped to the parent folder
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_item_launch_derives_processing_context_from_parent_folder(
    server, owner, parentFolder, launchItem
):
    # The manifest's config.json points at the item's parent folder...
    manifest = _get(server, ITEM_MANIFEST_PATH % launchItem["_id"], owner)
    config_url = next(
        r["url"] for r in manifest.json["resources"] if r["name"] == "config.json"
    )
    assert _segment_after(config_url, "folder") == str(parentFolder["_id"])

    # ...and that config advertises a processing provider whose baseUrl is the
    # parent folder's volview_processing surface — no item-scoped route exists.
    config = _get(server, CONFIG_PATH % parentFolder["_id"], owner)
    provider = config.json["processing"]["providers"][0]
    assert provider["baseUrl"] == (
        "/api/v1/folder/%s/volview_processing" % parentFolder["_id"]
    )


# ---------------------------------------------------------------------------
# 2. listTasks — reachable at the derived parent folder; ACL still gates
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_list_tasks_under_item_launch_reaches_parent_folder(
    server, owner, parentFolder, launchItem, monkeypatch
):
    summary = {
        "id": "task-1", "title": "Seg", "description": "d", "dockerImage": "img",
    }
    monkeypatch.setattr(processing, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(processing, "_scopedCliItems", lambda user: [object()])
    monkeypatch.setattr(processing, "_cliItemToSummary", lambda c: summary)

    folder_id = _served_launch_folder_id(server, launchItem, owner)
    assert folder_id == str(parentFolder["_id"])

    resp = _get(server, TASKS_PATH % folder_id, owner)
    assert resp.output_status.startswith(b"200")
    assert resp.json == [summary]


@pytest.mark.plugin("volview")
def test_list_tasks_under_item_launch_still_enforces_folder_acl(
    server, owner, stranger, parentFolder, launchItem
):
    # The derived route is folder-addressed, so the parent folder's READ ACL is
    # the boundary — a stranger who cannot read the private parent folder is
    # refused, never served another study's catalog (fail closed).
    resp = server.request(
        path=TASKS_PATH % parentFolder["_id"], method="GET", user=stranger,
        isJson=False, exception=True,
    )
    assert resp.output_status.startswith(b"403")


# ---------------------------------------------------------------------------
# 3. runTask — launch stamp + default output folder are the parent folder
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_run_task_under_item_launch_stamps_and_outputs_to_parent_folder(
    server, owner, parentFolder, launchItem, runStub
):
    from girder_jobs.models.job import Job

    folder_id = _served_launch_folder_id(server, launchItem, owner)
    assert folder_id == str(parentFolder["_id"])

    resp = server.request(
        path=RUN_PATH % folder_id, method="POST", user=owner,
        body=json.dumps({"values": {"outputVolume": {"name": "result.nrrd"}}}),
        type="application/json",
        additionalHeaders=[("Sec-Fetch-Site", "same-origin")],
        isJson=True, exception=True,
    )
    assert resp.output_status.startswith(b"200")

    # Default output folder (no explicit folderRef) == the item's parent folder.
    assert runStub["params"]["outputVolume"] == "result.nrrd"
    assert runStub["params"]["outputVolume_folder"] == str(parentFolder["_id"])

    # The job's launch context stamp (Chunk 19, D5) is the SAME derived parent
    # folder, so tier-2 listRecentJobs scoping stays coherent for item launches.
    job = Job().load(resp.json["jobId"], force=True)
    assert job[processing._LAUNCH_FOLDER_FIELD] == str(parentFolder["_id"])


# ---------------------------------------------------------------------------
# 4. stageInput — the transient input lands in the parent folder
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_stage_under_item_launch_lands_in_parent_folder(
    server, owner, parentFolder, launchItem
):
    from girder.models.file import File
    from girder.models.item import Item

    folder_id = _served_launch_folder_id(server, launchItem, owner)
    assert folder_id == str(parentFolder["_id"])

    resp = server.request(
        path=STAGE_PATH % folder_id, method="POST", user=owner,
        params={"name": "seg.seg.nrrd"}, body=b"seg-bytes",
        type="application/octet-stream",
        additionalHeaders=[("Sec-Fetch-Site", "same-origin")],
        isJson=True, exception=True,
    )
    assert resp.output_status.startswith(b"200")

    file_id = processing._fileIdFromMintedUri(resp.json["uris"][0])
    staged_item = Item().load(File().load(file_id, force=True)["itemId"], force=True)
    # Launch-context-scoped staging: the transient item is a child of the item's
    # parent folder, and is tagged transient like any other staged input.
    assert str(staged_item["folderId"]) == str(parentFolder["_id"])
    assert staged_item["meta"]["volviewTransient"] is True
