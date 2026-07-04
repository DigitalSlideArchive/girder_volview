"""Server-fixture coverage for Chunk 21 item (a): proxiable download headers.

The proxiable download route passes ``headers=False`` to ``File().download`` so an
S3 assetstore can proxy/redirect -- which also suppressed Girder's default download
headers, leaving a proxied file served with cherrypy's ``text/html`` default and no
``Content-Disposition`` (the outlier this chunk closes). The fix always serves
``Content-Disposition: attachment`` + an inert ``application/octet-stream`` so a
proxied file can never render inline in a browser.

Server-side only and client-transparent: the engine's ``$fetch`` reads the response
body regardless of these headers; only browser navigation is affected.

Needs a live pytest-girder Mongo; self-skips offline like the other route tests.
"""

import io
import os
import socket

import pytest


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
    reason="needs a live pytest-girder Mongo (like test_input_resolution_routes); "
    "unavailable offline",
)


PROXIABLE_PATH = "/file/%s/proxiable/%s"


# ---------------------------------------------------------------------------
# Real user / folder / file
# ---------------------------------------------------------------------------

@pytest.fixture
def owner(db):
    from girder.models.user import User
    return User().createUser(
        login="proxyowner", password="password123", firstName="A", lastName="B",
        email="proxyowner@example.com", admin=False,
    )


@pytest.fixture
def ownerFolder(fsAssetstore, owner):
    from girder.models.folder import Folder
    return Folder().createFolder(
        owner, "launch", parentType="user", creator=owner, public=False
    )


def _upload(user, folder, name, content=b"pixel-bytes"):
    from girder.models.upload import Upload
    return Upload().uploadFromFile(
        io.BytesIO(content), size=len(content), name=name,
        parentType="folder", parent=folder, user=user,
    )


def _download(server, file, user, name="scan.nrrd", headers=None):
    return server.request(
        path=PROXIABLE_PATH % (file["_id"], name),
        method="GET", user=user, isJson=False,
        additionalHeaders=headers, exception=True,
    )


# ---------------------------------------------------------------------------
# The proxied path always forces a safe download
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_proxiable_download_forces_attachment_and_inert_type(server, owner, ownerFolder):
    f = _upload(owner, ownerFolder, "scan.nrrd")

    resp = _download(server, f, owner)

    assert resp.output_status.startswith(b"200")
    disposition = resp.headers.get("Content-Disposition", "")
    assert disposition.startswith("attachment")
    assert "scan.nrrd" in disposition
    assert resp.headers.get("Content-Type", "").startswith("application/octet-stream")


@pytest.mark.plugin("volview")
def test_proxiable_range_request_keeps_safe_headers(server, owner, ownerFolder):
    # A partial (Range) request still carries the safe headers alongside the 206,
    # so a browser can never be talked into rendering a proxied slice inline.
    f = _upload(owner, ownerFolder, "scan.nrrd", content=b"0123456789")

    resp = _download(server, f, owner, headers=[("Range", "bytes=0-3")])

    assert resp.output_status.startswith(b"206")
    assert resp.headers.get("Content-Disposition", "").startswith("attachment")
    assert resp.headers.get("Content-Type", "").startswith("application/octet-stream")
