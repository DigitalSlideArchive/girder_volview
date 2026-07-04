"""Server-fixture coverage for Seam 1 input resolution (WORKORDER chunk 9).

What the offline ``test_input_value_resolution`` unit tests cannot show, exercised
here against real Girder models + the live cherrypy pipeline:

1. *Real ACL* -- a file the submitting user genuinely cannot read is rejected 403
   by the Girder permission check (the security boundary), not by a stub.
2. *End-to-end submit* -- a POST of client-minted ``{type, uris}`` values to the
   real ``runTask`` route resolves the facade's own URIs back to file ids and
   forwards the (comma-joined) ids to the CLI (b3). The slicer_cli_web docker job
   is stubbed (no docker in CI); everything up to and including the param the CLI
   would receive is real.

Like ``test_csrf_routes`` this needs a live pytest-girder server + Mongo; the
module self-skips when the test Mongo is unreachable so the offline gate stays
green, and runs (and must pass) wherever Mongo is present.
"""

import io
import json
import os
import socket
import types

import pytest
from bson.objectid import ObjectId

import contract_loader
from girder_volview.facade import processing
from girder_volview.utils import makeFileDownloadUrl


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
    "<executable><category>Radiology</category><title>Median</title>"
    "<parameters>"
    "<image><name>inputVolume</name><channel>input</channel></image>"
    "</parameters></executable>"
)

RUN_PATH = "/folder/%s/volview_processing/tasks/sometask/run"


# ---------------------------------------------------------------------------
# Real users / folders / files
# ---------------------------------------------------------------------------

def _makeUser(login, email):
    from girder.models.user import User
    return User().createUser(
        login=login, password="password123", firstName="A", lastName="B",
        email=email, admin=False,
    )


@pytest.fixture
def owner(db):
    return _makeUser("owneruser", "owner@example.com")


@pytest.fixture
def stranger(db):
    return _makeUser("strangeruser", "stranger@example.com")


@pytest.fixture
def ownerFolder(fsAssetstore, owner):
    from girder.models.folder import Folder
    return Folder().createFolder(
        owner, "launch", parentType="user", creator=owner, public=False
    )


@pytest.fixture
def strangerFolder(fsAssetstore, stranger):
    from girder.models.folder import Folder
    return Folder().createFolder(
        stranger, "strangerlaunch", parentType="user", creator=stranger,
        public=False,
    )


def _upload(user, folder, name, content=b"pixel-bytes"):
    from girder.models.upload import Upload
    return Upload().uploadFromFile(
        io.BytesIO(content), size=len(content), name=name,
        parentType="folder", parent=folder, user=user,
    )


@pytest.fixture
def stubCli(monkeypatch):
    """Stub the slicer_cli_web touch points so runTask reaches (and past) input
    resolution without docker; capture the params the CLI would receive."""
    captured = {}
    cli = types.SimpleNamespace(name="Median", xml=_CLI_XML)
    monkeypatch.setattr(processing, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(processing, "_findScopedCliItem", lambda taskId, user: cli)

    def fake_gen(cliItem, params, user):
        captured["params"] = dict(params)
        return {"_id": ObjectId()}

    monkeypatch.setattr(processing, "_genDockerJob", fake_gen)
    return captured


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


# ---------------------------------------------------------------------------
# Helper-level: real ACL re-check under the submitting user
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_owner_uris_resolve_to_their_file_ids(server, owner, ownerFolder):
    f1 = _upload(owner, ownerFolder, "1-001.dcm")
    f2 = _upload(owner, ownerFolder, "1-002.dcm")
    value = {
        "type": "image",
        "format": "dicom-series",
        "uris": [makeFileDownloadUrl(f1), makeFileDownloadUrl(f2)],
    }
    params = processing._translateValuesToSlicerParams(
        {"inputVolume": value}, user=owner, folder=ownerFolder
    )
    assert params["inputVolume"] == "%s,%s" % (f1["_id"], f2["_id"])


@pytest.mark.plugin("volview")
def test_foreign_uri_rejected_400(server, owner, ownerFolder):
    from girder.exceptions import RestException
    value = {
        "type": "image",
        "uris": ["https://evil.example/api/v1/file/%s/proxiable/x" % ObjectId()],
    }
    with pytest.raises(RestException) as exc:
        processing._translateValuesToSlicerParams(
            {"inputVolume": value}, user=owner, folder=ownerFolder
        )
    assert exc.value.code == 400


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
        processing._translateValuesToSlicerParams(
            {"inputVolume": value}, user=stranger, folder=ownerFolder
        )


# ---------------------------------------------------------------------------
# End-to-end: POST client-minted URIs -> ids -> job (the chunk-9 acceptance)
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_runtask_end_to_end_minted_uris_to_job(server, owner, ownerFolder, stubCli):
    f1 = _upload(owner, ownerFolder, "1-001.dcm")
    f2 = _upload(owner, ownerFolder, "1-002.dcm")
    f3 = _upload(owner, ownerFolder, "1-003.dcm")
    # Built from the Chunk-5 dicom-series input-value fixture shape, with real
    # facade-minted URIs substituted for the fixture's placeholder ids.
    fixture = contract_loader.load_fixture("wire/input-value.dicom-series.json")
    value = {**fixture, "uris": [makeFileDownloadUrl(f) for f in (f1, f2, f3)]}

    resp = _run(server, ownerFolder, owner, {"inputVolume": value})

    assert resp.output_status.startswith(b"200")
    assert resp.json["jobId"]
    # b3: the CLI receives the resolved file ids as one comma-joined <string>.
    assert stubCli["params"]["inputVolume"] == "%s,%s,%s" % (
        f1["_id"], f2["_id"], f3["_id"]
    )


@pytest.mark.plugin("volview")
def test_runtask_single_file_input_to_job(server, owner, ownerFolder, stubCli):
    f1 = _upload(owner, ownerFolder, "scan.nrrd")
    value = {"type": "image", "format": "nrrd", "uris": [makeFileDownloadUrl(f1)]}

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


# ---------------------------------------------------------------------------
# Chunk 21 item (b): submit-boundary reserved-param deny-list (fail closed, 400)
#
# A separate defense from the spec-side drop (the translator never emits these to
# the client form; test_slicer_spec_translation asserts that half). Here a crafted
# submit that feeds a reserved/undeclared param back in is rejected before any job
# is created -- and the facade's own derived {param}_folder plumbing still works.
# ---------------------------------------------------------------------------

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
    # The facade synthesizes {param}_folder server-side; a client-submitted
    # *_folder is undeclared and rejected (it would otherwise redirect where an
    # output is written or collide with the facade's own output plumbing).
    resp = _run(
        server, ownerFolder, owner,
        {"outputVolume_folder": str(ownerFolder["_id"])},
    )
    assert resp.output_status.startswith(b"400")
    assert "params" not in stubCli


@pytest.mark.plugin("volview")
def test_runtask_denylist_leaves_facade_output_folder_plumbing_intact(
    server, owner, ownerFolder, stubCli
):
    # The deny-list screens the RAW submission, so a legitimate output request
    # (key does not end in _folder) still yields the facade's derived
    # {param}_folder param pointing at the launch folder.
    f1 = _upload(owner, ownerFolder, "scan.nrrd")
    values = {
        "inputVolume": {"type": "image", "uris": [makeFileDownloadUrl(f1)]},
        "outputVolume": {"name": "result.nrrd"},
    }
    resp = _run(server, ownerFolder, owner, values)
    assert resp.output_status.startswith(b"200")
    assert stubCli["params"]["outputVolume"] == "result.nrrd"
    assert stubCli["params"]["outputVolume_folder"] == str(ownerFolder["_id"])
