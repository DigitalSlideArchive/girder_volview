"""The proxiable load-handle scheme round-trips.

Driven by the VolView backend-contract corpus
(``backend-contract/fixtures/wire/handle-roundtrip.json``, contract 0.4.0):
the handle module percent-encodes the file-name segment at mint and
unescapes it at parse, so

- ``parseFileHandle(mintFileHandle(fileId, name)) == (fileId, name)`` for
  every corpus name -- including ``#``, ``?``, ``%``, spaces and unicode;
- ``mintFileHandle(*parseFileHandle(handle)) == handle`` byte-for-byte for
  the corpus exemplar handles;
- every mint site routes through the ONE module
  (``utils.makeFileDownloadUrl`` is a delegate);
- legacy raw-name handles, persisted in records/scenes or held by a live
  client, still parse to the same file id;
- genuinely foreign shapes stay rejected.

Offline: pure string math, no Mongo.
"""

import pytest
from bson.objectid import ObjectId

from conftest import API_ROOT
import contract_loader
from girder_volview import handles
from girder_volview.backend import inputs
from girder_volview.utils import makeFileDownloadUrl


# The shared ``_fixed_api_root`` pin (conftest) keeps the corpus exemplars'
# ``/api/v1/...`` handles byte-for-byte comparable regardless of ambient
# server config.
pytestmark = pytest.mark.usefixtures("_fixed_api_root")


_CORPUS = contract_loader.load_fixture("wire/handle-roundtrip.json")
_CASES = [(case["name"], case["escaped"]) for case in _CORPUS["cases"]]
_CASE_IDS = [name for name, _ in _CASES]


@pytest.mark.parametrize("name,escaped", _CASES, ids=_CASE_IDS)
def test_mint_percent_encodes_the_name_segment(name, escaped):
    fileId = str(ObjectId())
    handle = handles.mintFileHandle(fileId, name)
    assert handle == "/%s/file/%s/proxiable/%s" % (API_ROOT, fileId, escaped)
    # No raw fragment/query delimiter (or space) ever rides the wire, so the
    # emitted handle survives URL contexts without the browser or an
    # intermediary splitting it.
    assert "#" not in handle
    assert "?" not in handle
    assert " " not in handle


@pytest.mark.parametrize("name,escaped", _CASES, ids=_CASE_IDS)
def test_parse_of_mint_round_trips_to_identical_parts(name, escaped):
    fileId = str(ObjectId())
    assert handles.parseFileHandle(handles.mintFileHandle(fileId, name)) == (
        fileId,
        name,
    )


@pytest.mark.parametrize("handle", _CORPUS["exemplarHandles"])
def test_mint_of_parse_round_trips_exemplar_handles_byte_for_byte(handle):
    parsed = handles.parseFileHandle(handle)
    assert parsed is not None
    assert handles.mintFileHandle(*parsed) == handle


@pytest.mark.parametrize("name,escaped", _CASES, ids=_CASE_IDS)
def test_make_file_download_url_delegates_to_the_handle_module(name, escaped):
    fileId = ObjectId()
    assert makeFileDownloadUrl({"_id": fileId, "name": name}) == handles.mintFileHandle(
        fileId, name
    )


@pytest.mark.parametrize("name,escaped", _CASES, ids=_CASE_IDS)
def test_file_id_from_minted_uri_reads_every_corpus_mint(name, escaped):
    fileId = str(ObjectId())
    minted = handles.mintFileHandle(fileId, name)
    assert inputs._fileIdFromMintedUri(minted) == fileId


@pytest.mark.parametrize(
    "rawName",
    [
        "brain.nrrd",  # clean names: old and new format coincide
        "left lung mask.seg.nrrd",  # raw space
        "Lesion #1.seg.nrrd",  # raw fragment delimiter
        "flow ?phase.nrrd",  # raw query delimiter
    ],
)
def test_legacy_raw_name_handles_still_resolve_the_same_file(rawName):
    # Legacy handles embed the raw name and are already persisted (job stamps,
    # held client echoes), so parse accepts any single-segment tail and
    # recovers the id.
    fileId = str(ObjectId())
    legacy = "/%s/file/%s/proxiable/%s" % (API_ROOT, fileId, rawName)
    parsed = handles.parseFileHandle(legacy)
    assert parsed is not None
    assert parsed[0] == fileId
    assert inputs._fileIdFromMintedUri(legacy) == fileId


# A FIXED placeholder id keeps these parametrize values byte-identical across
# pytest-xdist workers. Minting a fresh ObjectId() at collection time gives every
# worker a different test id, which aborts the whole `-n auto` run with "Different
# tests were collected between gw*". The id itself is irrelevant here — each URI is
# rejected on its shape/scheme, not on id validity.
_PLACEHOLDER_ID = "0123456789abcdef01234567"


@pytest.mark.parametrize(
    "uri",
    [
        None,
        123,
        "",
        "file/%s/proxiable/x" % _PLACEHOLDER_ID,  # not origin-relative
        "https://evil.example/api/v1/file/%s/proxiable/x" % _PLACEHOLDER_ID,
        "/api/v1/item/%s/download" % _PLACEHOLDER_ID,  # wrong resource
        "/api/v1/file/%s/download/x" % _PLACEHOLDER_ID,  # wrong verb
        "/api/v1/file/notanobjectid/proxiable/x",  # non-id
        "/api/v1/file/%s/proxiable/" % _PLACEHOLDER_ID,  # empty name
        "/api/v1/file/%s/proxiable/a/b" % _PLACEHOLDER_ID,  # extra path segment
        "/api/v1/file//proxiable/x",  # missing id
        "volview-backend:girder/file/%s" % _PLACEHOLDER_ID,  # scheme A, not a handle
    ],
)
def test_foreign_shapes_are_rejected_by_parse(uri):
    assert handles.parseFileHandle(uri) is None
