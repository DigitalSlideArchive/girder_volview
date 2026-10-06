import io
import re

from conftest import mongo_reachable

import pytest

import girder_volview


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo; unavailable offline",
)


LOADABLE_PATH = "/folder/%s/volview_loadable"


@pytest.fixture
def studyFolder(fsAssetstore, owner):
    from girder.models.folder import Folder

    return Folder().createFolder(
        owner, "study", parentType="user", creator=owner, public=False
    )


@pytest.fixture
def subFolder(studyFolder, owner):
    from girder.models.folder import Folder

    return Folder().createFolder(
        studyFolder, "nested", parentType="folder", creator=owner, public=False
    )


def _upload(folder, user, name, mimeType=None):
    """Upload one file into ``folder``; return its file document."""
    from girder.models.upload import Upload

    data = b"pixels"
    return Upload().uploadFromFile(
        io.BytesIO(data),
        size=len(data),
        name=name,
        parentType="folder",
        parent=folder,
        user=user,
        mimeType=mimeType,
    )


def _loadable(server, folder, user):
    return server.request(
        path=LOADABLE_PATH % folder["_id"], method="GET", user=user, isJson=True
    )


@pytest.fixture
def inspectFiles(monkeypatch):
    """Capture the file docs the aggregation hands to the post-filter.

    ``hasLoadableFile`` receives the aggregation cursor; replacement records one
    materialized list per call and then defers to the real predicate so the route
    still answers normally. ``calls`` holds every captured batch.
    """
    real = girder_volview.hasLoadableFile
    calls = []

    def spy(files, user=None):
        docs = list(files)
        calls.append(docs)
        return real(docs, user=user)

    monkeypatch.setattr(girder_volview, "hasLoadableFile", spy)
    return calls


def _onlyBatch(calls):
    # The route invokes the post-filter exactly once per request; returning that
    # one materialized batch keeps every assertion from silently passing on an
    # empty ``calls`` list.
    assert len(calls) == 1, "post-filter calls: %d" % len(calls)
    return calls[0]


@pytest.mark.plugin("volview")
def test_loadable_folder_finds_file_in_deep_descendant(
    server, owner, studyFolder, subFolder
):
    # Baseline: the aggregation still walks past the requested folder into
    # descendants, so the file's own folder name never appears in the route URL.
    _upload(subFolder, owner, "scan.nrrd")

    assert _loadable(server, studyFolder, owner).json == {"loadable": True}


@pytest.mark.plugin("volview")
def test_match_drops_non_loadable_file_before_the_post_filter(
    server, owner, studyFolder, inspectFiles
):
    # notes.txt is neither a loadable extension nor a loadable mime. The $match
    # must remove it from the pipeline, not merely let the post-filter reject it:
    # assert the post-filter saw zero documents.
    _upload(studyFolder, owner, "notes.txt", mimeType="text/plain")

    assert _loadable(server, studyFolder, owner).json == {"loadable": False}
    assert _onlyBatch(inspectFiles) == []


@pytest.mark.plugin("volview")
def test_match_keeps_file_by_loadable_mime_without_loadable_name(
    server, owner, studyFolder, inspectFiles
):
    # A non-matching name with a loadable mime still passes the $match (and is
    # loadable to the post-filter): the filter is an OR, not a name-only gate.
    _upload(studyFolder, owner, "scan.bin", mimeType="image/png")

    assert _loadable(server, studyFolder, owner).json == {"loadable": True}
    assert [doc["name"] for doc in _onlyBatch(inspectFiles)] == ["scan.bin"]


@pytest.mark.plugin("volview")
def test_match_excludes_attached_file_even_with_loadable_name(
    server, owner, studyFolder, inspectFiles
):
    # An attached file has no itemId, so the item->file $lookup alone would drop
    # it; the guard targets files that carry BOTH an itemId and attachedToId.
    # Stamp attachedToId onto a normally-uploaded, loadable-named file and assert
    # the $match removes it.
    from girder.models.file import File

    fileDoc = _upload(studyFolder, owner, "scan.nrrd")
    File().collection.update_one(
        {"_id": fileDoc["_id"]}, {"$set": {"attachedToId": studyFolder["_id"]}}
    )

    assert _loadable(server, studyFolder, owner).json == {"loadable": False}
    assert _onlyBatch(inspectFiles) == []


@pytest.mark.plugin("volview")
def test_limit_caps_documents_examined(
    server, owner, studyFolder, inspectFiles, monkeypatch
):
    # With the cap set below the number of loadable files, the post-filter must
    # never see more than the cap -- that is the bound on the unbounded work.
    monkeypatch.setattr(girder_volview, "VOLVIEW_LOADABLE_FILE_LIMIT", 3)
    for index in range(7):
        _upload(studyFolder, owner, "scan%d.nrrd" % index)

    assert _loadable(server, studyFolder, owner).json == {"loadable": True}
    assert len(_onlyBatch(inspectFiles)) == 3


@pytest.mark.plugin("volview")
def test_limit_does_not_hide_a_loadable_file_within_bounds(
    server, owner, studyFolder, monkeypatch
):
    # A file that is loadable to the post-filter but that the $match retains only
    # on its mime branch still counts, so long as it falls within the cap.
    monkeypatch.setattr(girder_volview, "VOLVIEW_LOADABLE_FILE_LIMIT", 2)
    _upload(studyFolder, owner, "notes.txt", mimeType="text/plain")
    _upload(studyFolder, owner, "scan.bin", mimeType="image/png")

    assert _loadable(server, studyFolder, owner).json == {"loadable": True}


def test_loadable_file_limit_is_positive():
    # A zero/negative cap would emit ``$limit: 0`` semantics that drop every
    # file, silently turning detection off for all folders.
    assert isinstance(girder_volview.VOLVIEW_LOADABLE_FILE_LIMIT, int)
    assert girder_volview.VOLVIEW_LOADABLE_FILE_LIMIT > 0


def test_loadable_name_re_anchors_loadable_and_session_extensions():
    # The $match regex is an end-anchored alternation over the loadable + session
    # extensions; it must match those and nothing else.
    pattern = girder_volview._LOADABLE_NAME_RE
    assert re.search(pattern, "brain.nrrd")
    assert re.search(pattern, "study.volview.zip")
    assert re.search(pattern, "caps.NII.GZ") is None  # exact suffix, case-sensitive
    assert re.search(pattern, "notes.txt") is None
    # The suffix is required, not merely present in the middle.
    assert re.search(pattern, "brain.nrrd.bak") is None
    # A name ending in a loadable extension matches regardless of a leading path.
    assert re.search(pattern, "nested/brain.nrrd")
