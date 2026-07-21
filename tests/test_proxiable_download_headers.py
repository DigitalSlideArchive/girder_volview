"""Server-fixture coverage for proxiable download headers.

The proxiable download route passes ``headers=False`` to ``File().download`` so an
S3 assetstore can proxy/redirect, which also suppresses Girder's default download
headers -- leaving a proxied file with cherrypy's ``text/html`` default and no
``Content-Disposition``. The route therefore always serves
``Content-Disposition: attachment`` + an inert ``application/octet-stream`` so a
proxied file can never render inline in a browser.

Server-side only and client-transparent: the engine's ``$fetch`` reads the response
body regardless of these headers; only browser navigation is affected.

Needs a live pytest-girder Mongo; self-skips offline like the other route tests.
"""

import io
from conftest import mongo_reachable

import pytest


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_input_resolution_routes); "
    "unavailable offline",
)


PROXIABLE_PATH = "/file/%s/proxiable/%s"


# Shared owner/ownerFolder fixtures live in conftest.
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


def _download(server, file, user, name="scan.nrrd", headers=None):
    return server.request(
        path=PROXIABLE_PATH % (file["_id"], name),
        method="GET",
        user=user,
        isJson=False,
        additionalHeaders=headers,
        exception=True,
    )


@pytest.mark.plugin("volview")
def test_proxiable_download_forces_attachment_and_inert_type(
    server, owner, ownerFolder
):
    f = _upload(owner, ownerFolder, "scan.nrrd")

    resp = _download(server, f, owner)

    assert resp.output_status.startswith(b"200")
    disposition = resp.headers.get("Content-Disposition", "")
    assert disposition.startswith("attachment")
    assert "scan.nrrd" in disposition
    assert resp.headers.get("Content-Type", "").startswith("application/octet-stream")


@pytest.mark.plugin("volview")
def test_minted_handle_path_serves_a_reserved_char_named_file(
    server, owner, ownerFolder
):
    # The emitted load handle percent-encodes the name segment, so the exact
    # path portion of the backend's OWN mint must serve the bytes -- no browser
    # fragment/query splitting, no 404.
    from girder_volview.utils import makeFileDownloadUrl

    f = _upload(owner, ownerFolder, "scan #2 ?phase.nrrd", content=b"abc")

    handle = makeFileDownloadUrl(f)
    assert handle.startswith("/api/v1/")
    assert "%23" in handle  # '#' never rides raw
    assert "%3F" in handle  # '?' never rides raw
    resp = server.request(
        path=handle[len("/api/v1") :],
        method="GET",
        user=owner,
        isJson=False,
        exception=True,
    )
    assert resp.output_status.startswith(b"200")
    assert b"".join(resp.body) == b"abc"


@pytest.mark.plugin("volview")
def test_proxiable_range_request_keeps_safe_headers(server, owner, ownerFolder):
    # A partial (Range) request still carries the safe headers alongside the 206,
    # so a browser can never be talked into rendering a proxied slice inline.
    f = _upload(owner, ownerFolder, "scan.nrrd", content=b"0123456789")

    resp = _download(server, f, owner, headers=[("Range", "bytes=0-3")])

    assert resp.output_status.startswith(b"206")
    assert resp.headers.get("Content-Disposition", "").startswith("attachment")
    assert resp.headers.get("Content-Type", "").startswith("application/octet-stream")
