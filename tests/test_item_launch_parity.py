"""Server-fixture coverage for item-launch parity, against real Girder models +
the live cherrypy pipeline.

A single-item launch runs the whole job flow: the backend derives the launch
context from the item's PARENT folder. There is no item-scoped processing route.
An item-launched client only reads the processing provider ``baseUrl`` served
over the trusted ``config=`` channel, which the launcher scopes to the item's
parent folder (``open.js`` builds ``configParam(item.folderId)``; the composed
item manifest carries no config resource, so that URL is the one and only config
channel) -> ``/folder/{parentId}/volview_config/...`` ->
``buildProcessingConfigBlock`` -> ``_providerBaseUrl(parentFolder)``. So every
test below *derives* the launch folder from the served config exactly as the
client would, then drives the three launch-context-scoped routes -- ``listTasks``
/ ``runTask`` / ``stageInput`` -- at that derived folder and asserts parity with
the parent folder.

Needs a live pytest-girder Mongo; self-skips when unreachable so the offline gate
stays green.
"""

import json
from conftest import mongo_reachable
import types

import pytest

from girder_volview.backend import inputs, outputs, routes, slicer_spec, submit


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_staging_routes); "
    "unavailable offline",
)


# A minimal CLI with one output param so runTask's translate step sets the
# default output folder ({param}_folder).
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


# The shared ``owner``/``stranger`` fixtures live in conftest.


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
        io.BytesIO(b"pixels"),
        size=6,
        name="brain.nrrd",
        parentType="folder",
        parent=parentFolder,
        user=owner,
    )
    return Item().load(file_doc["itemId"], force=True)


@pytest.fixture
def runStub(monkeypatch):
    """Stub slicer_cli_web so runTask reaches job creation without docker, and
    CAPTURE the translated params so the default-output-folder can be asserted.

    A REAL Girder job is created with the prepared initial fields so the launch
    association can be read back.
    """
    cli = types.SimpleNamespace(name="Seg", xml=_CLI_XML)
    captured = {}
    monkeypatch.setattr(submit, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(
        submit,
        "_findScopedCliItem",
        lambda taskId, user: (cli, slicer_spec.parse_cli(cli.xml)),
    )

    def fake_gen(cliItem, params, user, initialFields):
        from girder_jobs.models.job import Job

        captured["params"] = params
        return Job().createJob(
            title="stub",
            type="volview_test",
            user=user,
            public=False,
            otherFields=initialFields,
        )

    monkeypatch.setattr(routes, "_genDockerJob", fake_gen)
    return captured


def _get(server, path, user):
    return server.request(
        path=path,
        method="GET",
        user=user,
        isJson=True,
        exception=True,
    )


def _segment_after(url, key):
    parts = url.strip("/").split("/")
    return parts[parts.index(key) + 1]


def _served_launch_folder_id(server, item, user):
    """Reproduce, exactly as the client does, the item -> parent-folder launch
    derivation: follow the launcher's trusted ``config=`` URL to the config route
    and return the folder id the served processing provider is scoped to.
    """
    config_folder_id = str(item["folderId"])
    config = _get(server, CONFIG_PATH % config_folder_id, user)
    provider = config.json["processing"]["providers"][0]
    # The baseUrl is the only processing handle the client is ever given.
    return _segment_after(provider["baseUrl"], "folder")


@pytest.mark.plugin("volview")
def test_item_launch_derives_processing_context_from_parent_folder(
    server, owner, parentFolder, launchItem
):
    # The item manifest is compose-direct (filesToManifest -> {"resources": [...]})
    # and must load for a plain image item...
    manifest = _get(server, ITEM_MANIFEST_PATH % launchItem["_id"], owner)
    assert "resources" in manifest.json

    # ...while the launcher-scoped parent-folder config advertises a processing
    # provider whose baseUrl is the parent folder's volview_processing surface --
    # no item-scoped route exists.
    config = _get(server, CONFIG_PATH % parentFolder["_id"], owner)
    provider = config.json["processing"]["providers"][0]
    assert provider["baseUrl"] == (
        "/api/v1/folder/%s/volview_processing" % parentFolder["_id"]
    )


@pytest.mark.plugin("volview")
def test_list_tasks_under_item_launch_reaches_parent_folder(
    server, owner, parentFolder, launchItem, monkeypatch
):
    summary = {
        "id": "task-1",
        "title": "Seg",
        "description": "d",
        "dockerImage": "img",
    }
    monkeypatch.setattr(submit, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(submit, "_scopedCliItems", lambda user: [object()])
    monkeypatch.setattr(submit, "_cliItemToSummary", lambda c: summary)

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
        path=TASKS_PATH % parentFolder["_id"],
        method="GET",
        user=stranger,
        isJson=False,
        exception=True,
    )
    assert resp.output_status.startswith(b"403")


@pytest.mark.plugin("volview")
def test_run_task_under_item_launch_stamps_and_outputs_to_parent_folder(
    server, owner, parentFolder, launchItem, runStub
):
    from girder_jobs.models.job import Job

    folder_id = _served_launch_folder_id(server, launchItem, owner)
    assert folder_id == str(parentFolder["_id"])

    resp = server.request(
        path=RUN_PATH % folder_id,
        method="POST",
        user=owner,
        body=json.dumps({"values": {"outputVolume": {"name": "result.nrrd"}}}),
        type="application/json",
        isJson=True,
        exception=True,
    )
    assert resp.output_status.startswith(b"200")

    # The output name is SERVER-OWNED: the client-supplied "result.nrrd" is
    # discarded and the deterministic server basename wins (no path traversal).
    serverName = "output.Seg.outputVolume.nii.gz"
    # The output is forced into the job's private output folder -- a child of the
    # derived launch (parent) folder -- NOT the parent folder itself.
    assert runStub["params"]["outputVolume"] == serverName
    outputFolderId = runStub["params"]["outputVolume_folder"]
    assert outputFolderId != str(parentFolder["_id"])

    # The job's launch context stamp is the SAME derived parent folder, so
    # durable job-history scoping stays coherent for item launches.
    job = Job().load(resp.json["jobId"], force=True)
    assert job[inputs._LAUNCH_FOLDER_FIELD] == str(parentFolder["_id"])
    assert job["volviewSubmissionId"]
    assert job[outputs._OUTPUTS_FIELD] == {}
    # The job OWNS that output folder (its sole correlation + ownership key); the
    # folder the output was forced into is exactly the one the job owns...
    assert job[outputs._OUTPUT_FOLDER_ID_FIELD] == outputFolderId
    # ...and it is a child of the derived launch/parent folder.
    from girder.models.folder import Folder

    outputFolder = Folder().load(outputFolderId, force=True)
    container = Folder().load(outputFolder["parentId"], force=True)
    assert container["name"] == routes.JOBS_CONTAINER_NAME
    assert str(container["parentId"]) == str(parentFolder["_id"])
    assert job[outputs._OUTPUT_SPECS_FIELD][0]["name"] == "outputVolume"
    assert job["volviewSubmittedParameters"] == {
        "outputVolume": {"name": serverName}
    }


@pytest.mark.plugin("volview")
def test_stage_under_item_launch_lands_in_parent_folder(
    server, owner, parentFolder, launchItem
):
    import json
    from girder.models.file import File
    from girder.models.item import Item
    from girder_volview.utils import makeFileDownloadUrl

    folder_id = _served_launch_folder_id(server, launchItem, owner)
    assert folder_id == str(parentFolder["_id"])

    reference_file = File().findOne({"itemId": launchItem["_id"]})
    boundary = "volview-item-stage-boundary"
    descriptor = json.dumps(
        {
            "type": "labelmap",
            "name": "seg.seg.nrrd",
            "referenceImage": {
                "type": "image",
                "uris": [makeFileDownloadUrl(reference_file)],
            },
        }
    ).encode("utf8")
    body = b"".join(
        [
            ("--%s\r\n" % boundary).encode(),
            b'Content-Disposition: form-data; name="descriptor"\r\n\r\n',
            descriptor,
            b"\r\n",
            ("--%s\r\n" % boundary).encode(),
            b'Content-Disposition: form-data; name="file"; filename="seg.seg.nrrd"\r\n',
            b"Content-Type: application/octet-stream\r\n\r\n",
            b"seg-bytes\r\n",
            ("--%s--\r\n" % boundary).encode(),
        ]
    )
    resp = server.request(
        path=STAGE_PATH % folder_id,
        method="POST",
        user=owner,
        body=body,
        type="multipart/form-data; boundary=%s" % boundary,
        isJson=True,
        exception=True,
    )
    assert resp.output_status.startswith(b"200")

    file_id = inputs._fileIdFromMintedUri(resp.json["uris"][0])
    staged_item = Item().load(File().load(file_id, force=True)["itemId"], force=True)
    # Launch-context-scoped staging: the transient item is a child of the item's
    # parent folder, and is tagged transient like any other staged input.
    assert str(staged_item["folderId"]) == str(parentFolder["_id"])
    assert staged_item["meta"]["volviewTransient"] is True
