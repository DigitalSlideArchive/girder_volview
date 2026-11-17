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

    def spy(files, user=None, *, batchParents=False):
        docs = list(files)
        calls.append(docs)
        return real(docs, user=user, batchParents=batchParents)

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


@pytest.mark.plugin("volview")
@pytest.mark.parametrize(
    "name,marker,expected",
    [
        ("scan.nrrd", None, True),
        ("scan.tif", None, True),
        ("scan.tif", "largeImage", False),
        ("scan.dcm", None, True),
        ("scan.dcm", "sm", False),
        ("scan.nrrd", "transient", False),
        ("scan.nrrd", "jobOutput", False),
        ("saved.volview.zip", "transient", True),
        ("saved.volview.zip", "jobOutput", True),
    ],
)
def test_non_admin_folder_loadability_preserves_image_and_working_data_rules(
    server, owner, stranger, studyFolder, name, marker, expected
):
    from girder.constants import AccessType
    from girder.models.folder import Folder
    from girder.models.item import Item
    from girder_volview.utils import (
        JOB_OUTPUT_FOLDER_META_KEY,
        TRANSIENT_STAGED_META_KEY,
    )

    assert not stranger["admin"]
    Folder().setUserAccess(studyFolder, stranger, AccessType.READ, save=True)
    file = _upload(studyFolder, owner, name)
    item = Item().load(file["itemId"], force=True)
    if marker == "largeImage":
        Item().collection.update_one(
            {"_id": item["_id"]}, {"$set": {"largeImage": {"fileId": file["_id"]}}}
        )
    elif marker == "sm":
        Item().setMetadata(item, {"dicom": {"Modality": "SM"}})
    elif marker == "transient":
        Item().setMetadata(item, {TRANSIENT_STAGED_META_KEY: True})
    elif marker == "jobOutput":
        Folder().setMetadata(studyFolder, {JOB_OUTPUT_FOLDER_META_KEY: True})

    assert _loadable(server, studyFolder, stranger).json == {"loadable": expected}


@pytest.mark.plugin("volview")
@pytest.mark.parametrize("anonymous", [False, True])
def test_folder_loadability_checks_each_descendants_read_access(
    server, owner, stranger, studyFolder, anonymous
):
    from girder.models.folder import Folder

    assert not stranger["admin"]
    Folder().setPublic(studyFolder, True, save=True)
    private = Folder().createFolder(
        studyFolder, "private", parentType="folder", creator=owner, public=False
    )
    _upload(private, owner, "private.nrrd")
    user = None if anonymous else stranger
    assert _loadable(server, studyFolder, user).json == {"loadable": False}

    public = Folder().createFolder(
        private, "public", parentType="folder", creator=owner, public=True
    )
    _upload(public, owner, "public.nrrd")
    assert _loadable(server, studyFolder, user).json == {"loadable": True}


@pytest.mark.plugin("volview")
def test_folder_loadability_observes_group_access_changes(
    server, owner, stranger, studyFolder, subFolder
):
    from girder.constants import AccessType
    from girder.models.folder import Folder
    from girder.models.group import Group
    from girder.models.item import Item

    Folder().setPublic(studyFolder, True, save=True)
    rejected = _upload(studyFolder, owner, "slide.dcm")
    Item().setMetadata(
        Item().load(rejected["itemId"], force=True), {"dicom": {"Modality": "SM"}}
    )
    _upload(subFolder, owner, "scan.nrrd")
    group = Group().createGroup("readers", creator=owner)
    Group().addUser(group, stranger)

    assert _loadable(server, studyFolder, stranger).json == {"loadable": False}
    Folder().setGroupAccess(subFolder, group, AccessType.READ, save=True)
    assert _loadable(server, studyFolder, stranger).json == {"loadable": True}
    assert _loadable(server, studyFolder, None).json == {"loadable": False}
    Folder().setGroupAccess(subFolder, group, None, save=True)
    assert _loadable(server, studyFolder, stranger).json == {"loadable": False}


@pytest.mark.plugin("volview")
@pytest.mark.parametrize(
    "name,parent,field,value",
    [
        ("scan.nrrd", "item", "meta.volviewTransient", True),
        ("scan.nrrd", "folder", "meta.volviewJobOutputFolder", True),
        ("scan.dcm", "item", "meta.dicom.Modality", "SM"),
        ("scan.tif", "item", "largeImage", None),
    ],
)
def test_folder_loadability_observes_parent_marker_changes(
    server, owner, stranger, studyFolder, name, parent, field, value
):
    from girder.constants import AccessType
    from girder.models.folder import Folder
    from girder.models.item import Item

    Folder().setUserAccess(studyFolder, stranger, AccessType.READ, save=True)
    file = _upload(studyFolder, owner, name)
    assert _loadable(server, studyFolder, stranger).json == {"loadable": True}

    if parent == "item":
        model, docId = Item(), file["itemId"]
    else:
        model, docId = Folder(), studyFolder["_id"]
    if field == "largeImage":
        value = {"fileId": file["_id"]}
    model.collection.update_one({"_id": docId}, {"$set": {field: value}})
    assert _loadable(server, studyFolder, stranger).json == {"loadable": False}

    model.collection.update_one({"_id": docId}, {"$unset": {field: ""}})
    assert _loadable(server, studyFolder, stranger).json == {"loadable": True}


@pytest.mark.plugin("volview")
def test_folder_loadability_handles_multiple_files_in_one_item(
    server, owner, studyFolder
):
    from girder.models.file import File
    from girder.models.item import Item

    tiff = _upload(studyFolder, owner, "slide.tif")
    Item().collection.update_one(
        {"_id": tiff["itemId"]}, {"$set": {"largeImage": {"fileId": tiff["_id"]}}}
    )
    image = {key: value for key, value in tiff.items() if key != "_id"}
    image.update(name="scan.nrrd", mimeType="application/octet-stream")
    File().collection.insert_one(image)

    assert _loadable(server, studyFolder, owner).json == {"loadable": True}


@pytest.mark.plugin("volview")
@pytest.mark.parametrize("rejectedCount", [1, 127, 128, 129])
def test_folder_loadability_finds_image_after_rejected_candidates(
    server, owner, studyFolder, subFolder, rejectedCount
):
    from bson import ObjectId
    from girder.models.file import File
    from girder.models.item import Item

    tiff = _upload(studyFolder, owner, "slide.tif")
    item = Item().load(tiff["itemId"], force=True)
    item["largeImage"] = {"fileId": tiff["_id"]}
    Item().collection.update_one(
        {"_id": item["_id"]}, {"$set": {"largeImage": item["largeImage"]}}
    )
    items, files = [], []
    for index in range(1, rejectedCount):
        copiedItem = dict(item, _id=ObjectId(), name=f"slide-{index}")
        copiedFile = dict(tiff, _id=ObjectId(), itemId=copiedItem["_id"])
        items.append(copiedItem)
        files.append(copiedFile)
    if items:
        Item().collection.insert_many(items)
        File().collection.insert_many(files)
    _upload(subFolder, owner, "scan.nrrd")

    assert _loadable(server, studyFolder, owner).json == {"loadable": True}


@pytest.mark.plugin("volview")
def test_folder_loadability_applies_output_markers_per_folder_across_batches(
    server, owner, stranger, studyFolder, subFolder
):
    from bson import ObjectId
    from girder.constants import AccessType
    from girder.models.file import File
    from girder.models.folder import Folder
    from girder.models.item import Item
    from girder_volview.utils import JOB_OUTPUT_FOLDER_META_KEY

    for folder in (studyFolder, subFolder):
        Folder().setUserAccess(folder, stranger, AccessType.READ, save=True)
    Folder().setMetadata(studyFolder, {JOB_OUTPUT_FOLDER_META_KEY: True})
    outputFile = _upload(studyFolder, owner, "output.nrrd")
    outputItem = Item().load(outputFile["itemId"], force=True)
    items, files = [], []
    for index in range(129):
        item = dict(outputItem, _id=ObjectId(), name=f"output-{index}")
        items.append(item)
        files.append(dict(outputFile, _id=ObjectId(), itemId=item["_id"]))
    Item().collection.insert_many(items)
    File().collection.insert_many(files)
    _upload(subFolder, owner, "scan.nrrd")

    assert _loadable(server, studyFolder, stranger).json == {"loadable": True}
    Folder().setMetadata(subFolder, {JOB_OUTPUT_FOLDER_META_KEY: True})
    assert _loadable(server, studyFolder, stranger).json == {"loadable": False}
    Folder().setMetadata(studyFolder, {JOB_OUTPUT_FOLDER_META_KEY: False})
    assert _loadable(server, studyFolder, stranger).json == {"loadable": True}


@pytest.mark.plugin("volview")
@pytest.mark.parametrize("name,expected", [("orphan.nrrd", False),
                                           ("saved.volview.zip", True)])
def test_folder_loadability_preserves_orphan_file_handling(
    server, owner, studyFolder, name, expected
):
    from girder.models.file import File

    File().collection.insert_one({"name": name, "mimeType": "application/octet-stream"})

    assert _loadable(server, studyFolder, owner).json == {"loadable": expected}


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
