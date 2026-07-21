"""Server-fixture coverage for input-values resolution, against real Girder
models + the live cherrypy pipeline: a file the submitting user cannot read is
rejected 403 by the real Girder permission check, and a POST of client-minted
``{type, uris}`` values to ``runTask`` resolves the backend's own URIs back to
file ids and forwards them comma-joined to the CLI. The slicer_cli_web docker job
is stubbed (no docker in CI); everything up to the param the CLI would receive is
real.

Needs a live pytest-girder server + Mongo; self-skips when the test Mongo is
unreachable so the offline gate stays green.
"""

import io
import json
from conftest import mongo_reachable
import types

import pytest
from bson.objectid import ObjectId

import contract_loader
from girder_volview.backend import inputs, routes, slicer_spec, submit
from girder_volview.utils import makeFileDownloadUrl


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo; unavailable offline",
)


_CLI_XML = (
    '<?xml version="1.0"?>'
    "<executable><category>Radiology</category><title>Median</title>"
    "<parameters>"
    "<image><name>inputVolume</name><channel>input</channel></image>"
    '<image type="label"><name>outputVolume</name><channel>output</channel></image>'
    "</parameters></executable>"
)

RUN_PATH = "/folder/%s/volview_processing/tasks/sometask/run"


# Shared owner/stranger/ownerFolder fixtures live in conftest.
@pytest.fixture
def strangerFolder(fsAssetstore, stranger):
    from girder.models.folder import Folder

    return Folder().createFolder(
        stranger,
        "strangerlaunch",
        parentType="user",
        creator=stranger,
        public=False,
    )


def _upload(user, folder, name, content=b"pixel-bytes"):
    from girder.models.upload import Upload

    return Upload().uploadFromFile(
        io.BytesIO(content),
        size=len(content),
        name=name,
        parentType="folder",
        parent=folder,
        user=user,
    )


@pytest.fixture
def stubCli(monkeypatch):
    """Stub the slicer_cli_web touch points so runTask reaches (and past) input
    resolution without docker; capture the params the CLI would receive."""
    captured = {}
    cli = types.SimpleNamespace(name="Median", xml=_CLI_XML)
    monkeypatch.setattr(submit, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(
        submit,
        "_findScopedCliItem",
        lambda taskId, user: (cli, slicer_spec.parse_cli(cli.xml)),
    )

    def fake_gen(cliItem, params, user, initialFields):
        captured["params"] = dict(params)
        return {"_id": ObjectId()}

    monkeypatch.setattr(routes, "_genDockerJob", fake_gen)
    return captured


def _run(server, folder, user, values):
    return server.request(
        path=RUN_PATH % folder["_id"],
        method="POST",
        user=user,
        body=json.dumps({"values": values}),
        type="application/json",
        isJson=True,
        exception=True,
    )


@pytest.mark.plugin("volview")
def test_owner_uris_resolve_to_their_file_ids(server, owner, ownerFolder):
    f1 = _upload(owner, ownerFolder, "1-001.dcm")
    f2 = _upload(owner, ownerFolder, "1-002.dcm")
    value = {
        "type": "image",
        "format": "dicom-series",
        "uris": [makeFileDownloadUrl(f1), makeFileDownloadUrl(f2)],
    }
    params, _ = submit._translateValuesToSlicerParams(
        {"inputVolume": value}, user=owner, outputFolder=ownerFolder
    )
    assert params["inputVolume"] == "%s,%s" % (f1["_id"], f2["_id"])


@pytest.mark.plugin("volview")
def test_stranger_cannot_resolve_owners_private_file_403(
    server, owner, stranger, ownerFolder
):
    from girder.exceptions import AccessException

    secret = _upload(owner, ownerFolder, "secret.dcm")  # owner's PRIVATE folder
    value = {"type": "image", "uris": [makeFileDownloadUrl(secret)]}
    # The id is recoverable from the path, but that is by design not a capability:
    # the stranger has no READ on the file, so resolution raises 403.
    with pytest.raises(AccessException):
        submit._translateValuesToSlicerParams(
            {"inputVolume": value}, user=stranger, outputFolder=ownerFolder
        )


@pytest.mark.plugin("volview")
def test_runtask_rejects_output_folder_ref(server, owner, ownerFolder, stubCli):
    # Output location is server-owned: a submitted folderRef on an output value
    # would redirect a job's outputs out of its own (correlation-key) folder, so
    # it 400s before any job is created.
    resp = _run(
        server,
        ownerFolder,
        owner,
        {"outputVolume": {"name": "result.nrrd", "folderRef": str(ownerFolder["_id"])}},
    )
    assert resp.output_status.startswith(b"400")
    assert "params" not in stubCli


@pytest.mark.plugin("volview")
def test_runtask_end_to_end_minted_uris_to_job(server, owner, ownerFolder, stubCli):
    f1 = _upload(owner, ownerFolder, "1-001.dcm")
    f2 = _upload(owner, ownerFolder, "1-002.dcm")
    f3 = _upload(owner, ownerFolder, "1-003.dcm")
    fixture = contract_loader.load_fixture("wire/input-value.dicom-series.json")
    value = {**fixture, "uris": [makeFileDownloadUrl(f) for f in (f1, f2, f3)]}

    resp = _run(server, ownerFolder, owner, {"inputVolume": value})

    assert resp.output_status.startswith(b"200")
    assert resp.json["jobId"]
    assert stubCli["params"]["inputVolume"] == "%s,%s,%s" % (
        f1["_id"],
        f2["_id"],
        f3["_id"],
    )


@pytest.mark.plugin("volview")
def test_runtask_resolves_all_input_files_in_one_batched_query(
    server, owner, ownerFolder, stubCli, monkeypatch
):
    from girder.models.file import File as GirderFile

    files = [_upload(owner, ownerFolder, "%d.dcm" % index) for index in range(3)]
    finds = []

    class BatchingFiles:
        def find(self, query=None, **kwargs):
            ids = ((query or {}).get("_id") or {}).get("$in", [])
            finds.append([str(fileId) for fileId in ids])
            return GirderFile().find(query=query, **kwargs)

    monkeypatch.setattr(inputs, "File", BatchingFiles)
    value = {
        "type": "image",
        "format": "dicom-series",
        "uris": [makeFileDownloadUrl(fileDoc) for fileDoc in files],
    }

    response = _run(server, ownerFolder, owner, {"inputVolume": value})

    assert response.output_status.startswith(b"200")
    # A many-slice series must NOT fan out to one File load per uri: input
    # resolution issues a SINGLE find carrying every id at once (no N+1).
    assert len(finds) == 1
    assert sorted(finds[0]) == sorted(str(fileDoc["_id"]) for fileDoc in files)
    assert stubCli["params"]["inputVolume"] == ",".join(
        str(fileDoc["_id"]) for fileDoc in files
    )


@pytest.mark.plugin("volview")
def test_runtask_single_file_input_to_job(server, owner, ownerFolder, stubCli):
    f1 = _upload(owner, ownerFolder, "scan.nrrd")
    value = {"type": "image", "format": "nrrd", "uris": [makeFileDownloadUrl(f1)]}

    resp = _run(server, ownerFolder, owner, {"inputVolume": value})

    assert resp.output_status.startswith(b"200")
    assert stubCli["params"]["inputVolume"] == str(f1["_id"])


@pytest.mark.plugin("volview")
def test_runtask_hash_named_file_submits_without_400(
    server, owner, ownerFolder, stubCli
):
    # '#'/'?' are legal in Girder file names, and the backend mints the input
    # handle itself, so its own mint must resolve at submit.
    f1 = _upload(owner, ownerFolder, "scan #2 ?phase.nrrd")
    value = {"type": "image", "uris": [makeFileDownloadUrl(f1)]}

    resp = _run(server, ownerFolder, owner, {"inputVolume": value})

    assert resp.output_status.startswith(b"200")
    assert stubCli["params"]["inputVolume"] == str(f1["_id"])


@pytest.mark.plugin("volview")
def test_runtask_foreign_uri_returns_400(server, owner, ownerFolder, stubCli):
    value = {
        "type": "image",
        "uris": ["https://evil.example/api/v1/file/%s/proxiable/x" % ObjectId()],
    }
    resp = _run(server, ownerFolder, owner, {"inputVolume": value})
    assert resp.output_status.startswith(b"400")
    assert "params" not in stubCli  # never reached job creation


@pytest.mark.plugin("volview")
def test_runtask_unreadable_file_returns_403(
    server, owner, stranger, ownerFolder, strangerFolder, stubCli
):
    secret = _upload(owner, ownerFolder, "secret.dcm")  # owner's private file
    value = {"type": "image", "uris": [makeFileDownloadUrl(secret)]}
    # The stranger has WRITE on their OWN launch folder (so the folder modelParam
    # passes) but no READ on the owner's file -> the ACL re-check 403s.
    resp = _run(server, strangerFolder, stranger, {"inputVolume": value})
    assert resp.output_status.startswith(b"403")
    assert "params" not in stubCli


# A defense separate from the spec-side drop: a crafted submit that feeds a
# reserved/undeclared param back in is rejected before any job is created, while
# the backend's own derived {param}_folder plumbing still works.
@pytest.mark.plugin("volview")
@pytest.mark.parametrize("reservedKey", ["girderApiUrl", "girderToken"])
def test_runtask_rejects_reserved_credential_param(
    server, owner, ownerFolder, stubCli, reservedKey
):
    # slicer_cli_web injects girderApiUrl/girderToken below the line; a client
    # value would try to redirect the CLI's girder client or swap its token.
    resp = _run(server, ownerFolder, owner, {reservedKey: "https://evil.example"})
    assert resp.output_status.startswith(b"400")
    assert "params" not in stubCli  # never reached job creation


@pytest.mark.plugin("volview")
def test_runtask_rejects_undeclared_output_folder_param(
    server, owner, ownerFolder, stubCli
):
    # The backend synthesizes {param}_folder server-side; a client-submitted
    # *_folder is undeclared and rejected (it would otherwise redirect where an
    # output is written or collide with the backend's own output plumbing).
    resp = _run(
        server,
        ownerFolder,
        owner,
        {"outputVolume_folder": str(ownerFolder["_id"])},
    )
    assert resp.output_status.startswith(b"400")
    assert "params" not in stubCli


@pytest.mark.plugin("volview")
def test_runtask_denylist_leaves_backend_output_folder_plumbing_intact(
    server, owner, ownerFolder, stubCli
):
    # The deny-list screens the RAW submission, so a legitimate output request
    # (key does not end in _folder) still yields the backend's derived
    # {param}_folder param, pointing at the job's private output folder (a marked
    # child of the launch folder), not the launch folder itself.
    from girder.models.folder import Folder
    from girder_volview.utils import JOB_OUTPUT_FOLDER_META_KEY

    f1 = _upload(owner, ownerFolder, "scan.nrrd")
    values = {
        "inputVolume": {"type": "image", "uris": [makeFileDownloadUrl(f1)]},
        "outputVolume": {"name": "result.nrrd"},
    }
    resp = _run(server, ownerFolder, owner, values)
    assert resp.output_status.startswith(b"200")
    # The output name is SERVER-OWNED: the client's "result.nrrd" is discarded and
    # the deterministic server basename wins (no client-controlled path). The
    # {param}_folder plumbing still points at the job's private output folder.
    assert stubCli["params"]["outputVolume"] == "scan.Median.outputVolume.nii.gz"
    outputFolderId = stubCli["params"]["outputVolume_folder"]
    assert outputFolderId != str(ownerFolder["_id"])
    outputFolder = Folder().load(outputFolderId, force=True)
    container = Folder().load(outputFolder["parentId"], force=True)
    assert container["name"] == routes.JOBS_CONTAINER_NAME
    assert str(container["parentId"]) == str(ownerFolder["_id"])
    assert outputFolder["meta"][JOB_OUTPUT_FOLDER_META_KEY] is True
