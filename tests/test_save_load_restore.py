"""Save / load / restore round-trip for the session zip, exercising
``backend/launch.py`` through the live cherrypy pipeline:

- raw checked picks ALWAYS open fresh: checking images is the "start fresh"
  gesture, so no saved session is ever substituted;
- a checked session item opens through to exactly that session, while a filter
  gesture resumes its newest matching session;
- a bare folder-open resumes the folder's newest ``session.volview.zip`` (by
  ``created``), else its raw loadable images;
- a filter-gesture save carries ``meta.linkedResources.filter`` and is excluded
  from the bare open; a plain save is resumed;
- a save returns a ``resumeUrl`` the client repoints its ``urls=`` at, and that
  url round-trips the saved zip byte-for-byte.
"""

import datetime
import json
import re
from conftest import _folderManifest, _itemManifest, _uploadFile, mongo_reachable

import pytest

from girder_volview.utils import makeFileDownloadUrl


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo; unavailable offline",
)


@pytest.fixture
def folder(ownerFolder):
    return ownerFolder


def _ageFile(fileDoc, hours):
    """Backdate a file's ``created`` so newest-by-created is deterministic."""
    from girder.models.file import File

    fileDoc["created"] = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    return File().save(fileDoc)


def _resourceNames(resp):
    return [resource["name"] for resource in resp.json["resources"]]


def _saveToFolder(server, folder, user, zipBytes, linkedResources):
    return server.request(
        path="/folder/%s/volview" % folder["_id"],
        method="POST",
        user=user,
        body=zipBytes,
        type="application/zip",
        isJson=True,
        exception=True,
        params={"metadata": json.dumps({"linkedResources": linkedResources})},
    )


def _saveToItem(server, item, user, zipBytes):
    return server.request(
        path="/item/%s/volview" % item["_id"],
        method="POST",
        user=user,
        body=zipBytes,
        type="application/zip",
        isJson=True,
        exception=True,
    )


def _downloadBytes(fileDoc):
    from girder.models.file import File

    return b"".join(File().download(fileDoc, headers=False)())


def test_save_without_content_length_is_the_clean_rejection_not_500(monkeypatch):
    # A header-less save (Transfer-Encoding: chunked) or a garbage header value
    # takes the same typed rejection as an empty body — never int(None)'s
    # TypeError surfacing as an opaque 500.
    import cherrypy
    from girder.exceptions import GirderException

    from girder_volview.backend import launch

    for headers in ({}, {"Content-Length": "abc"}):
        monkeypatch.setattr(cherrypy.request, "headers", headers, raising=False)
        with pytest.raises(GirderException):
            launch._uploadWholeSession(None, "id", None, "err.identifier")


@pytest.mark.plugin("volview")
def test_single_raw_item_opens_fresh_not_session(server, owner, folder):
    rawItem, rawFile = _uploadFile(folder, owner, "brain.nrrd")
    # A saved session exists in the same folder; the single-item open ignores it.
    _uploadFile(folder, owner, "session.volview.zip", data=b"zip")

    resp = _itemManifest(server, rawItem, owner, exception=True)
    names = _resourceNames(resp)
    assert "brain.nrrd" in names
    assert not any(n.endswith(".volview.zip") for n in names)


@pytest.mark.plugin("volview")
def test_checked_pick_ignores_unrelated_session(server, owner, folder):
    itemA, fileA = _uploadFile(folder, owner, "a.nrrd")
    itemB, fileB = _uploadFile(folder, owner, "b.nrrd")
    # A session without matching linkedResources must not be substituted.
    _uploadFile(folder, owner, "session.volview.zip", data=b"zip")

    resp = _folderManifest(
        server,
        folder,
        owner,
        params={"items": "%s,%s" % (itemA["_id"], itemB["_id"]), "folders": ""},
        exception=True,
    )
    names = _resourceNames(resp)
    assert set(names) == {"a.nrrd", "b.nrrd", "config.json"}
    assert not any(n.endswith(".volview.zip") for n in names)


@pytest.mark.plugin("volview")
def test_filter_pick_ignores_unrelated_session(server, owner, folder):
    _uploadFile(folder, owner, "keep.nrrd", meta={"pick": "yes"})
    _uploadFile(folder, owner, "drop.nrrd", meta={"pick": "no"})
    _uploadFile(folder, owner, "session.volview.zip", data=b"zip")

    resp = _folderManifest(
        server,
        folder,
        owner,
        params={"filters": json.dumps([{"meta.pick": "yes"}])},
        exception=True,
    )
    names = _resourceNames(resp)
    assert "keep.nrrd" in names
    assert "drop.nrrd" not in names
    assert not any(n.endswith(".volview.zip") for n in names)


@pytest.mark.plugin("volview")
def test_filter_pick_includes_files_the_loadable_gate_would_drop(server, owner, folder):
    # A filter row owns its matched files: an extensionless slice (no loadable
    # extension, octet-stream mime) still belongs to the manifest. Only working
    # data — transient staged inputs and session zips — is excluded.
    from girder_volview.utils import TRANSIENT_STAGED_META_KEY

    _uploadFile(folder, owner, "slice001", meta={"pick": "yes"})
    _uploadFile(folder, owner, "keep.nrrd", meta={"pick": "yes"})
    _uploadFile(
        folder,
        owner,
        "staged.nrrd",
        meta={"pick": "yes", TRANSIENT_STAGED_META_KEY: True},
    )
    _uploadFile(folder, owner, "old.volview.zip", data=b"zip", meta={"pick": "yes"})

    resp = _folderManifest(
        server,
        folder,
        owner,
        params={"filters": json.dumps([{"meta.pick": "yes"}])},
        exception=True,
    )
    names = _resourceNames(resp)
    assert "slice001" in names
    assert "keep.nrrd" in names
    assert "staged.nrrd" not in names
    assert not any(n.endswith(".volview.zip") for n in names)


@pytest.mark.plugin("volview")
def test_checked_raw_pick_opens_fresh_despite_matching_session(server, owner, folder):
    # Checking raw images is the "start fresh" gesture: even a NEWER save
    # recorded against exactly this selection set is not substituted.
    itemA, _ = _uploadFile(folder, owner, "a.nrrd")
    itemB, _ = _uploadFile(folder, owner, "b.nrrd")
    linked = {
        "items": [str(itemA["_id"]), str(itemB["_id"])],
        "folders": [],
    }
    _saveToFolder(server, folder, owner, b"annotated", linked)

    resp = _folderManifest(
        server,
        folder,
        owner,
        params={"items": ",".join(linked["items"]), "folders": ""},
        exception=True,
    )

    names = _resourceNames(resp)
    assert "a.nrrd" in names
    assert "b.nrrd" in names
    assert not any(".volview.zip" in n for n in names)


@pytest.mark.plugin("volview")
def test_filter_pick_resumes_newest_matching_session(server, owner, folder):
    filter_ = [{"meta.pick": "yes"}]
    _uploadFile(folder, owner, "keep.nrrd", meta={"pick": "yes"})
    _uploadFile(folder, owner, "drop.nrrd", meta={"pick": "no"})
    _saveToFolder(server, folder, owner, b"annotated", {"filter": filter_})

    resp = _folderManifest(
        server,
        folder,
        owner,
        params={"filters": json.dumps(filter_)},
        exception=True,
    )

    names = _resourceNames(resp)
    assert len([name for name in names if name.endswith(".volview.zip")]) == 1
    assert "keep.nrrd" not in names


@pytest.mark.plugin("volview")
def test_checked_session_item_opens_saved_state(server, owner, folder):
    item, _ = _uploadFile(folder, owner, "brain.nrrd")
    saveResp = _saveToFolder(
        server,
        folder,
        owner,
        b"annotated",
        {"items": [str(item["_id"])], "folders": []},
    )
    sessionId = _itemIdFromResume(saveResp.json["resumeUrl"])

    resp = _folderManifest(
        server,
        folder,
        owner,
        params={"items": sessionId, "folders": ""},
        exception=True,
    )

    names = _resourceNames(resp)
    assert names.count("session.volview.zip") == 1
    assert "brain.nrrd" not in names


def _sessionDownloadUrls(resp):
    return [
        r["url"] for r in resp.json["resources"] if ".volview.zip" in r["name"]
    ]


def _itemFileDownloadUrl(itemId):
    from girder.models.item import Item

    fileDoc = next(iter(Item().childFiles(Item().load(itemId, force=True))))
    return makeFileDownloadUrl(fileDoc)


@pytest.mark.plugin("volview")
def test_checked_old_session_opens_that_session_not_newest(server, owner, folder):
    # With several saves accumulated in the folder, explicitly checking an OLD
    # session item opens through to exactly that session -- it is never
    # re-matched to the newest sibling save.
    itemA, _ = _uploadFile(folder, owner, "a.nrrd")
    linked = {"items": [str(itemA["_id"])], "folders": []}
    r1 = _saveToFolder(server, folder, owner, b"first", linked)
    s1Id = _itemIdFromResume(r1.json["resumeUrl"])
    r2 = _saveToFolder(server, folder, owner, b"second", linked)
    s2Id = _itemIdFromResume(r2.json["resumeUrl"])
    assert s2Id != s1Id

    resp = _folderManifest(
        server, folder, owner, params={"items": s1Id, "folders": ""}, exception=True
    )
    assert _sessionDownloadUrls(resp) == [_itemFileDownloadUrl(s1Id)]


@pytest.mark.plugin("volview")
def test_checked_old_filter_session_opens_that_session_not_newest(
    server, owner, folder
):
    # The same gesture for filter-linked sessions: checking an old filter save
    # opens it, even though re-entering the filter row itself resumes the
    # newest.
    filter_ = [{"meta.pick": "yes"}]
    _uploadFile(folder, owner, "keep.nrrd", meta={"pick": "yes"})
    r1 = _saveToFolder(server, folder, owner, b"first", {"filter": filter_})
    s1Id = _itemIdFromResume(r1.json["resumeUrl"])
    r2 = _saveToFolder(server, folder, owner, b"second", {"filter": filter_})
    s2Id = _itemIdFromResume(r2.json["resumeUrl"])
    assert s2Id != s1Id

    resp = _folderManifest(
        server, folder, owner, params={"items": s1Id, "folders": ""}, exception=True
    )
    assert _sessionDownloadUrls(resp) == [_itemFileDownloadUrl(s1Id)]

    # The filter row itself still resumes the newest matching save.
    resp = _folderManifest(
        server, folder, owner, params={"filters": json.dumps(filter_)}, exception=True
    )
    assert _sessionDownloadUrls(resp) == [_itemFileDownloadUrl(s2Id)]


@pytest.mark.plugin("volview")
def test_filter_session_resolving_to_no_files_falls_back_to_fresh(
    server, owner, folder, monkeypatch
):
    # A matched filter session that resolves to NO loadable files must take the
    # fresh leg, not emit a manifest of nothing but config.json. Guards the
    # empty-list-vs-None distinction: getFilteredSessionFile returns None for
    # "no session matched" but [] for "matched, nothing loadable", and gating on
    # `is None` let [] through as a resolved session -- a permanently blank
    # viewer with no gesture that recovers it.
    from girder_volview.backend import launch

    filter_ = [{"meta.pick": "yes"}]
    _uploadFile(folder, owner, "keep.nrrd", meta={"pick": "yes"})
    _saveToFolder(server, folder, owner, b"annotated", {"filter": filter_})

    monkeypatch.setattr(
        launch, "getFilteredSessionFile", lambda folder, filters, user: []
    )
    resp = _folderManifest(
        server, folder, owner, params={"filters": json.dumps(filter_)}, exception=True
    )

    names = _resourceNames(resp)
    assert "keep.nrrd" in names, "empty session must fall back to the filtered images"
    assert names != ["config.json"]


@pytest.mark.plugin("volview")
def test_save_with_non_dict_metadata_does_not_orphan_an_item(server, owner, folder):
    # jsonParam hands back whatever the client sent. A list must not reach the
    # post-upload `.get`, where the AttributeError would 500 only AFTER the zip
    # was stored -- reporting failure while leaving an orphan session item.
    from girder.models.folder import Folder as FolderModel

    resp = server.request(
        path="/folder/%s/volview" % folder["_id"],
        method="POST",
        user=owner,
        body=b"annotated",
        type="application/zip",
        isJson=True,
        exception=True,
        params={"metadata": json.dumps([{"linkedResources": {}}])},
    )

    assert "resumeUrl" in resp.json
    sessions = [
        item
        for item in FolderModel().childItems(folder)
        if item["name"].endswith(".volview.zip")
    ]
    assert len(sessions) == 1, "exactly the one saved session, no orphan"


@pytest.mark.plugin("volview")
def test_save_with_non_dict_linked_resources_does_not_orphan_an_item(
    server, owner, folder
):
    # Same stance one level down: a truthy non-object `linkedResources` must not
    # reach `.get` after the zip stored. The rebase is resolved before the upload,
    # so an unlinkable shape just saves without lineage.
    from girder.models.folder import Folder as FolderModel

    resp = server.request(
        path="/folder/%s/volview" % folder["_id"],
        method="POST",
        user=owner,
        body=b"annotated",
        type="application/zip",
        isJson=True,
        exception=True,
        params={"metadata": json.dumps({"linkedResources": "nonsense"})},
    )

    assert "resumeUrl" in resp.json
    sessions = [
        item
        for item in FolderModel().childItems(folder)
        if item["name"].endswith(".volview.zip")
    ]
    assert len(sessions) == 1, "exactly the one saved session, no orphan"


@pytest.mark.plugin("volview")
def test_failed_metadata_write_removes_the_session_item(
    server, owner, folder, monkeypatch
):
    # An unstamped session item is still the folder's NEWEST session, so a later
    # folder-open would restore this failed save. The save must roll it back.
    from girder.models.folder import Folder as FolderModel
    from girder.models.item import Item as ItemModel

    def boom(self, item, metadata, **kwargs):
        raise Exception("metadata write failed")

    monkeypatch.setattr(ItemModel, "setMetadata", boom)

    resp = server.request(
        path="/folder/%s/volview" % folder["_id"],
        method="POST",
        user=owner,
        body=b"annotated",
        type="application/zip",
        isJson=True,
        exception=True,
        params={"metadata": json.dumps({"linkedResources": {}})},
    )

    assert resp.output_status.startswith(b"500")
    sessions = [
        item
        for item in FolderModel().childItems(folder)
        if item["name"].endswith(".volview.zip")
    ]
    assert sessions == [], "the failed save must not linger as a restore target"


@pytest.mark.plugin("volview")
def test_filters_must_be_json_object_or_array(server, owner, folder):
    resp = _folderManifest(
        server, folder, owner, params={"filters": json.dumps("not-a-dict")}
    )
    assert resp.output_status.startswith(b"400")


@pytest.mark.plugin("volview")
def test_checked_session_save_rebases_linked_resources_to_originals(
    server, owner, folder
):
    # A save made from a checked-session open rebases its linkedResources back
    # onto the session's own lineage: the new save records the ORIGINAL raw
    # selection, not {items:[S1]}, keeping the recorded selection truthful.
    from girder.models.item import Item

    itemA, _ = _uploadFile(folder, owner, "a.nrrd")
    itemB, _ = _uploadFile(folder, owner, "b.nrrd")
    originals = {"items": [str(itemA["_id"]), str(itemB["_id"])], "folders": []}

    r1 = _saveToFolder(server, folder, owner, b"first-save", originals)
    s1Id = _itemIdFromResume(r1.json["resumeUrl"])

    # Reopen S1 (check the session item) and save again. The client stamps
    # linkedResources={items:[S1]}; the rebase rewrites it to the originals.
    r2 = _saveToFolder(
        server, folder, owner, b"second-save", {"items": [s1Id], "folders": []}
    )
    s2Id = _itemIdFromResume(r2.json["resumeUrl"])
    assert s2Id != s1Id

    s2 = Item().load(s2Id, force=True)
    linked = s2.get("meta", {}).get("linkedResources", {})
    assert set(linked.get("items", [])) == set(originals["items"])


@pytest.mark.plugin("volview")
def test_checked_filter_session_save_inherits_filter_lineage(server, owner, folder):
    # A save made from a checked FILTER-session open inherits the filter link
    # (via the rebase), so the filter row resumes the new save and the bare
    # folder-open keeps excluding it.
    filter_ = [{"meta.pick": "yes"}]
    _uploadFile(folder, owner, "keep.nrrd", meta={"pick": "yes"})
    r1 = _saveToFolder(server, folder, owner, b"first", {"filter": filter_})
    s1Id = _itemIdFromResume(r1.json["resumeUrl"])

    # Reopen S1 by checking it, then save; metadata carries {items:[S1]}.
    r2 = _saveToFolder(
        server, folder, owner, b"second", {"items": [s1Id], "folders": []}
    )
    s2Id = _itemIdFromResume(r2.json["resumeUrl"])
    assert s2Id != s1Id

    # The filter row resumes the NEW save.
    resp = _folderManifest(
        server, folder, owner, params={"filters": json.dumps(filter_)}, exception=True
    )
    assert _sessionDownloadUrls(resp) == [_itemFileDownloadUrl(s2Id)]

    # The bare folder-open still excludes filter-linked saves.
    resp = _folderManifest(server, folder, owner, exception=True)
    assert not any(
        ".volview.zip" in r["name"] for r in resp.json["resources"]
    )


@pytest.mark.plugin("volview")
def test_explicit_selection_wins_over_filters(server, owner, folder):
    # A request carrying BOTH items= and filters= loads the checked items; the
    # filter set does not silently override the explicit selection.
    _uploadFile(folder, owner, "keep.nrrd", meta={"pick": "yes"})
    checked, _ = _uploadFile(folder, owner, "checked.nrrd", meta={"pick": "no"})

    resp = _folderManifest(
        server,
        folder,
        owner,
        params={
            "items": str(checked["_id"]),
            "folders": "",
            "filters": json.dumps([{"meta.pick": "yes"}]),
        },
        exception=True,
    )
    names = _resourceNames(resp)
    assert "checked.nrrd" in names
    assert "keep.nrrd" not in names


@pytest.mark.plugin("volview")
def test_bare_folder_resumes_newest_session(server, owner, folder):
    _uploadFile(folder, owner, "brain.nrrd")
    _, older = _uploadFile(folder, owner, "older.volview.zip", data=b"old")
    _, newer = _uploadFile(folder, owner, "newer.volview.zip", data=b"new")
    _ageFile(older, hours=2)

    resp = _folderManifest(server, folder, owner, exception=True)
    names = _resourceNames(resp)
    assert "newer.volview.zip" in names
    assert "older.volview.zip" not in names
    assert "brain.nrrd" not in names


@pytest.mark.plugin("volview")
def test_bare_folder_without_session_opens_raw_images(server, owner, folder):
    _, rawFile = _uploadFile(folder, owner, "brain.nrrd")

    resp = _folderManifest(server, folder, owner, exception=True)
    names = _resourceNames(resp)
    assert "brain.nrrd" in names
    assert any(
        r["name"] == "brain.nrrd" and r["url"] == makeFileDownloadUrl(rawFile)
        for r in resp.json["resources"]
    )


@pytest.mark.plugin("volview")
def test_filter_save_excluded_from_bare_open_plain_save_resumed(server, owner, folder):
    # Plain save first, then a NEWER filter-gesture save (stamps
    # meta.linkedResources.filter). The bare open excludes the newer filter save
    # and resumes the older plain save: the stamp decides, not recency.
    _saveToFolder(server, folder, owner, b"plain-zip", {"items": [], "folders": []})
    _saveToFolder(
        server, folder, owner, b"filter-zip", {"filter": [{"meta.pick": "yes"}]}
    )

    resp = _folderManifest(server, folder, owner, exception=True)
    session_names = [n for n in _resourceNames(resp) if n.endswith(".volview.zip")]
    assert session_names == ["session.volview.zip"]


def _itemIdFromResume(resumeUrl):
    match = re.search(r"/item/([^/]+)/volview$", resumeUrl or "")
    assert match, "resumeUrl is not an item/:id/volview URL: %r" % resumeUrl
    return match.group(1)


@pytest.mark.plugin("volview")
def test_folder_save_returns_only_resume_url_and_creates_session_item(
    server, owner, folder
):
    from girder.models.item import Item

    resp = _saveToFolder(
        server, folder, owner, b"scene-zip", {"items": [], "folders": []}
    )
    # The response is a SINGLE field -- the save/load URL -- and carries NO
    # girder ids: the VolView client stays opaque to the item id.
    assert set(resp.json.keys()) == {"resumeUrl"}
    newItemId = _itemIdFromResume(resp.json["resumeUrl"])
    item = Item().load(newItemId, force=True)
    assert item["name"] == "session.volview.zip"


@pytest.mark.plugin("volview")
def test_item_save_returns_resume_url_pointing_at_the_item(server, owner, folder):
    rawItem, _ = _uploadFile(folder, owner, "brain.nrrd")
    resp = _saveToItem(server, rawItem, owner, b"scene-zip")
    assert set(resp.json.keys()) == {"resumeUrl"}
    assert resp.json["resumeUrl"] == "/api/v1/item/%s/volview" % rawItem["_id"]


@pytest.mark.plugin("volview")
def test_repeat_folder_saves_accumulate_session_items(server, owner, folder):
    # The client repoints only its reload (urls=) after a save; the save target
    # stays folder-scoped, so every save mints a NEW session.volview.zip item in
    # the folder and a bare folder-open / F5 resumes the newest one.
    _uploadFile(folder, owner, "brain.nrrd")  # a raw image so the folder isn't empty
    r1 = _saveToFolder(server, folder, owner, b"first", {"items": [], "folders": []})
    firstId = _itemIdFromResume(r1.json["resumeUrl"])
    assert _sessionItemCount(folder) == 1

    r2 = _saveToFolder(server, folder, owner, b"second", {"items": [], "folders": []})
    secondId = _itemIdFromResume(r2.json["resumeUrl"])
    assert secondId != firstId
    assert _sessionItemCount(folder) == 2


def _sessionItemCount(folder):
    from girder.models.folder import Folder

    # Girder uniquifies duplicate item names ("session.volview.zip (1)"), so
    # match by substring like the plugin's isSessionItem does.
    return sum(
        1 for it in Folder().childItems(folder) if ".volview.zip" in it["name"]
    )


@pytest.mark.plugin("volview")
def test_resume_url_round_trips_saved_zip_byte_identical(server, owner, folder):
    from girder.models.file import File
    from girder.models.item import Item

    payload = b"the-exact-scene-bytes"
    saveResp = _saveToFolder(
        server, folder, owner, payload, {"items": [], "folders": []}
    )
    sessionItem = Item().load(_itemIdFromResume(saveResp.json["resumeUrl"]), force=True)

    resp = _itemManifest(server, sessionItem, owner, exception=True)
    names = _resourceNames(resp)
    assert any(n.endswith(".volview.zip") for n in names)

    stored = list(Item().childFiles(sessionItem))
    assert len(stored) == 1
    assert _downloadBytes(File().load(stored[0]["_id"], force=True)) == payload
