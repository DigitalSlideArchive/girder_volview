from bson.objectid import ObjectId

from girder_volview.facade.processing import (
    _looksLikeSourceRef,
    encodeSourceRef,
)


def test_file_source_ref_is_raw_girder_file_id():
    fileId = ObjectId()
    itemId = ObjectId()
    folderId = ObjectId()

    assert encodeSourceRef(
        fileId=fileId, itemId=itemId, folderId=folderId,
    ) == str(fileId)


def test_source_ref_detection_accepts_raw_and_typed_ids():
    fileId = ObjectId()

    assert _looksLikeSourceRef(str(fileId), "file")
    assert _looksLikeSourceRef(f"girder:file:{fileId}", "file")
    assert not _looksLikeSourceRef("not.a.signed.token", "file")
