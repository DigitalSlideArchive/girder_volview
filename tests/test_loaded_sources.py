"""Coverage for volume-aware ``loadedSources`` (D10 part 1, item 3.2).

The processing facade advertises one source per *volume* rather than one per
Girder item: it groups a folder's files by ``SeriesInstanceUID`` so a multi-slice
series surfaces as a single source carrying its whole (slice-ordered) file set,
while non-DICOM / ungroupable files stay one volume per file. These tests drive
``_loadedSourcesForFolder`` with fake Girder models (the module looks up
``Folder``/``Item``/``File`` lazily, so monkeypatching the module attributes is
enough — no live Girder needed, same spirit as ``test_processing_source_ref``).
"""

import pytest
from bson.objectid import ObjectId

from girder.exceptions import RestException

from girder_volview.facade import processing


# ---------------------------------------------------------------------------
# Fakes + builders
# ---------------------------------------------------------------------------

class _FakeFolderModel:
    def __init__(self, items, folderDoc=None):
        self._items = items
        self._folder = folderDoc

    def childItems(self, folder, user=None, limit=0):
        return list(self._items)

    def load(self, folderId, user=None, level=None, exc=False):
        return self._folder


class _FakeItemModel:
    def __init__(self, filesByItem):
        self._filesByItem = filesByItem

    def childFiles(self, item, limit=0):
        return list(self._filesByItem[item["_id"]])


class _FakeFileModel:
    def __init__(self, filesById):
        self._filesById = filesById

    def load(self, fileId, user=None, level=None, exc=False):
        return next(
            (doc for fid, doc in self._filesById.items() if str(fid) == str(fileId)),
            None,
        )


def _mkFile(name="f.dcm", dcm=None):
    """A fake file doc; ``_dcm`` is the per-file tag dict the parse stub returns."""
    return {"_id": ObjectId(), "name": name, "_dcm": dcm}


def _mkItem(name, files, metaDicom=None):
    meta = {"dicom": metaDicom} if metaDicom is not None else {}
    return {"_id": ObjectId(), "name": name, "meta": meta, "_files": files}


def _dcm(uid, instance=None, descr=None, ipp=None):
    tags = {"SeriesInstanceUID": uid}
    if instance is not None:
        tags["InstanceNumber"] = instance
    if descr is not None:
        tags["SeriesDescription"] = descr
    if ipp is not None:
        tags["ImagePositionPatient"] = ipp
    return tags


def _install(monkeypatch, items, folderDoc=None):
    filesByItem = {it["_id"]: it["_files"] for it in items}
    monkeypatch.setattr(
        processing, "Folder", lambda: _FakeFolderModel(items, folderDoc)
    )
    monkeypatch.setattr(processing, "Item", lambda: _FakeItemModel(filesByItem))
    # Multi-file (L2) items parse per-file tags; the stub returns each file's _dcm.
    monkeypatch.setattr(processing, "_parseFileDicomTags", lambda f: f.get("_dcm"))


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_l3_single_file_items_one_series_groups_to_one_volume(monkeypatch):
    # The usual case: one folder = many single-file items = one series. Tags come
    # from item.meta.dicom (the parse stub is not consulted for single-file items).
    uid = "1.2.3"
    fA, fB, fC = _mkFile("a.dcm"), _mkFile("b.dcm"), _mkFile("c.dcm")
    # Folder lists items out of slice order to prove the source is sorted.
    itB = _mkItem("b", [fB], metaDicom=_dcm(uid, instance=2, descr="CT Chest"))
    itA = _mkItem("a", [fA], metaDicom=_dcm(uid, instance=1, descr="CT Chest"))
    itC = _mkItem("c", [fC], metaDicom=_dcm(uid, instance=3, descr="CT Chest"))
    _install(monkeypatch, [itB, itA, itC])

    sources = processing._loadedSourcesForFolder({"_id": ObjectId()}, user=None)

    assert len(sources) == 1
    src = sources[0]
    assert src["fileIds"] == [str(fA["_id"]), str(fB["_id"]), str(fC["_id"])]
    assert src["matchKey"] == {
        "kind": "series",
        "seriesInstanceUID": uid,
        "seriesDescription": "CT Chest",
    }
    assert src["sourceRef"].startswith("series:")


def test_l2_single_item_many_files_one_series(monkeypatch):
    # L2: one item, many DICOM files. item.meta.dicom reflects only one
    # representative file, so per-file tags must be parsed to order the rest.
    uid = "9.9.9"
    fB = _mkFile("s2.dcm", dcm=_dcm(uid, instance=2))
    fA = _mkFile("s1.dcm", dcm=_dcm(uid, instance=1))
    fC = _mkFile("s3.dcm", dcm=_dcm(uid, instance=3))
    # Files listed out of order; item meta pretends every slice is instance 3 —
    # if grouping trusted item meta, the stable sort would keep [fB, fA, fC].
    it = _mkItem("series-item", [fB, fA, fC], metaDicom=_dcm(uid, instance=3))
    _install(monkeypatch, [it])

    sources = processing._loadedSourcesForFolder({"_id": ObjectId()}, user=None)

    assert len(sources) == 1
    assert sources[0]["fileIds"] == [str(fA["_id"]), str(fB["_id"]), str(fC["_id"])]
    assert sources[0]["matchKey"]["seriesInstanceUID"] == uid
    assert sources[0]["sourceRef"].startswith("series:")


def test_two_series_in_one_folder_yield_two_sources(monkeypatch):
    u1, u2 = "1.1", "2.2"
    f1a, f1b, f2a = _mkFile("1a.dcm"), _mkFile("1b.dcm"), _mkFile("2a.dcm")
    it1a = _mkItem("1a", [f1a], metaDicom=_dcm(u1, instance=1))
    it2a = _mkItem("2a", [f2a], metaDicom=_dcm(u2, instance=1))
    it1b = _mkItem("1b", [f1b], metaDicom=_dcm(u1, instance=2))
    _install(monkeypatch, [it1a, it2a, it1b])

    sources = processing._loadedSourcesForFolder({"_id": ObjectId()}, user=None)

    assert len(sources) == 2
    assert all("matchKey" in s and "fileIds" in s for s in sources)
    # Discovery order is preserved: series u1 was seen first.
    assert sources[0]["matchKey"]["seriesInstanceUID"] == u1
    assert sources[1]["matchKey"]["seriesInstanceUID"] == u2
    s1 = sources[0]
    assert s1["fileIds"] == [str(f1a["_id"]), str(f1b["_id"])]


def test_single_multiframe_dicom_item_is_one_source(monkeypatch):
    # L1: one item = one file = the whole volume (a multi-frame DICOM).
    uid = "mf.1"
    f = _mkFile("multiframe.dcm")
    it = _mkItem("multiframe.dcm", [f], metaDicom=_dcm(uid, instance=1, descr="4D"))
    _install(monkeypatch, [it])

    sources = processing._loadedSourcesForFolder({"_id": ObjectId()}, user=None)

    assert len(sources) == 1
    src = sources[0]
    assert src["fileIds"] == [str(f["_id"])]
    # A single-file series keeps the raw file-id ref: the file is the volume.
    assert src["sourceRef"] == str(f["_id"])
    assert src["matchKey"]["seriesInstanceUID"] == uid


def test_non_dicom_files_are_one_source_each(monkeypatch):
    # Non-DICOM whole-volume files: one source per file, keyed by name. This is
    # today's behavior and must keep working (single-file raw-id sourceRef).
    fn, fr = _mkFile("brain.nii.gz"), _mkFile("mask.nrrd")
    itn = _mkItem("brain.nii.gz", [fn])  # no meta.dicom
    itr = _mkItem("mask.nrrd", [fr])
    _install(monkeypatch, [itn, itr])

    sources = processing._loadedSourcesForFolder({"_id": ObjectId()}, user=None)

    assert len(sources) == 2
    assert sources[0]["matchKey"] == {"kind": "name", "name": "brain.nii.gz"}
    assert sources[0]["sourceRef"] == str(fn["_id"])
    assert sources[0]["fileIds"] == [str(fn["_id"])]
    assert sources[1]["matchKey"] == {"kind": "name", "name": "mask.nrrd"}


# ---------------------------------------------------------------------------
# Series sourceRef encode / decode / resolve
# ---------------------------------------------------------------------------

def test_series_source_ref_round_trips():
    folderId = ObjectId()
    uid = "1.2.840.10008.x"
    ref = processing.encodeSourceRef(seriesInstanceUID=uid, folderId=folderId)
    assert ref == f"series:{folderId}:{uid}"
    assert processing.decodeSeriesSourceRef(ref) == (str(folderId), uid)


def test_decode_rejects_non_series_refs():
    assert processing.decodeSeriesSourceRef(str(ObjectId())) is None
    assert processing.decodeSeriesSourceRef("series:onlyfolder") is None
    assert processing.decodeSeriesSourceRef("series::uid") is None
    assert processing.decodeSeriesSourceRef(None) is None


def test_encode_series_requires_folder():
    with pytest.raises(RestException):
        processing.encodeSourceRef(seriesInstanceUID="1.2")


def test_resolve_series_ref_returns_ordered_files(monkeypatch):
    uid = "r.e.s"
    folderId = ObjectId()
    fB = _mkFile("s2.dcm", dcm=_dcm(uid, instance=2))
    fA = _mkFile("s1.dcm", dcm=_dcm(uid, instance=1))
    it = _mkItem("series", [fB, fA], metaDicom=_dcm(uid, instance=2))
    _install(monkeypatch, [it], folderDoc={"_id": folderId})
    monkeypatch.setattr(
        processing, "File", lambda: _FakeFileModel({fB["_id"]: fB, fA["_id"]: fA})
    )

    ref = processing.encodeSourceRef(seriesInstanceUID=uid, folderId=folderId)
    files = processing.resolveSeriesSourceRefToFiles(ref, user=None)

    assert [f["_id"] for f in files] == [fA["_id"], fB["_id"]]


def test_resolve_rejects_non_series_ref():
    with pytest.raises(RestException):
        processing.resolveSeriesSourceRefToFiles(str(ObjectId()), user=None)
