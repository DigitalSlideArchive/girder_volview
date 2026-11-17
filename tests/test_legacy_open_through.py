"""Legacy ``session.volview.zip`` open-through against real Girder models.

Exercises the ordinary launch composer (``backend/launch.py``) through the
live cherrypy pipeline:

- **bare folder-open** resumes the folder's newest ``session.volview.zip`` as
  the byte-identical legacy ``{resources}`` manifest; with no zip it opens the
  folder's raw loadable images instead;
- **explicit zip open (item route):** a ``session.volview.zip`` item opens as
  its resources list (restore);
- **empty gesture** (no zip, no loadable images) returns a config-only
  manifest;
- **merely opening writes NOTHING:** GETs of both manifest routes mutate no
  folder/item/file doc (the read paths are read-only).
"""

from conftest import _folderManifest, _itemManifest, _uploadFile, mongo_reachable

import pytest

from girder_volview.utils import filesToManifest, makeFileDownloadUrl


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo; unavailable offline",
)


# Shared owner fixture + upload/manifest helpers live in conftest
@pytest.fixture
def studyFolder(fsAssetstore, owner):
    from girder.models.folder import Folder

    return Folder().createFolder(
        owner, "study", parentType="user", creator=owner, public=False
    )


def _legacyManifestFor(fileDoc, folder):
    """The byte-identical legacy ``{resources}`` manifest for one zip file."""
    return filesToManifest([(fileDoc["name"], fileDoc)], folder["_id"])


@pytest.mark.plugin("volview")
def test_no_native_snapshot_newest_zip_opens_byte_identical(server, owner, studyFolder):
    _, fileA = _uploadFile(studyFolder, owner, "brain.nrrd")
    _, zipOld = _uploadFile(studyFolder, owner, "old.volview.zip", data=b"oldzip")
    _, zipNew = _uploadFile(studyFolder, owner, "new.volview.zip", data=b"newzip")

    resp = _folderManifest(server, studyFolder, owner, exception=True)
    # Byte-identical legacy shape: the newest zip + the config entry, nothing
    # else.
    assert resp.json == _legacyManifestFor(zipNew, studyFolder)


@pytest.mark.plugin("volview")
def test_no_snapshot_no_zip_is_ephemeral_composed(server, owner, studyFolder):
    _, fileA = _uploadFile(studyFolder, owner, "brain.nrrd")

    resp = _folderManifest(server, studyFolder, owner, exception=True)
    # No session zip: the bare folder-open falls to the folder's raw loadable
    # images -- the legacy {resources} shape, the image + config entries only.
    resources = resp.json["resources"]
    names = [resource["name"] for resource in resources]
    assert "brain.nrrd" in names
    assert names[-1] == "config.json"
    assert any(
        resource["name"] == "brain.nrrd"
        and resource["url"] == makeFileDownloadUrl(fileA)
        for resource in resources
    )


@pytest.mark.plugin("volview")
def test_empty_folder_opens_config_only_manifest(server, owner, studyFolder):
    # No zip, no loadable images: the folder-open returns an empty manifest (only
    # the config.json resource); the launcher hides the button for such folders.
    resp = _folderManifest(server, studyFolder, owner, exception=True)
    assert [r["name"] for r in resp.json["resources"]] == ["config.json"]


@pytest.mark.plugin("volview")
def test_item_route_session_zip_opens_through(server, owner, studyFolder):
    from girder.models.item import Item

    _, zipFile = _uploadFile(studyFolder, owner, "old.volview.zip", data=b"zip")
    zipItem = Item().load(zipFile["itemId"], force=True)

    # Without the session-zip branch this would 400 at the resolver (session
    # files are not loadable bases): the item route opens the zip through as its
    # legacy resources list (restore).
    resp = _itemManifest(server, zipItem, owner, exception=True)
    assert resp.json == _legacyManifestFor(zipFile, studyFolder)


@pytest.mark.plugin("volview")
def test_opening_writes_nothing(server, owner, studyFolder):
    from girder.models.folder import Folder
    from girder.models.item import Item

    baseItem, fileA = _uploadFile(studyFolder, owner, "brain.nrrd")
    _, zipFile = _uploadFile(studyFolder, owner, "old.volview.zip", data=b"zip")
    zipItem = Item().load(zipFile["itemId"], force=True)

    before = {
        "folder": Folder().load(studyFolder["_id"], force=True),
        "baseItem": Item().load(baseItem["_id"], force=True),
        "zipItem": Item().load(zipItem["_id"], force=True),
    }

    # All three read shapes: the legacy-zip fallback open (folder route), the
    # explicit zip open (item route), and an ephemeral compose (image item).
    _folderManifest(server, studyFolder, owner, exception=True)
    _itemManifest(server, zipItem, owner, exception=True)
    _itemManifest(server, baseItem, owner, exception=True)

    assert Folder().load(studyFolder["_id"], force=True) == before["folder"]
    assert Item().load(baseItem["_id"], force=True) == before["baseItem"]
    assert Item().load(zipItem["_id"], force=True) == before["zipItem"]
