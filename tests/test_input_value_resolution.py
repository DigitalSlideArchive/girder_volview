"""Seam 1, facade half (WORKORDER chunk 9): the facade resolves its OWN minted
URI scheme back to Girder file ids and forwards them to the CLI as a ``<string>``
param (D10 v1 = b3). These offline unit tests drive the pure resolver + the
values→params translation with the Chunk-5 golden ``input-value`` fixtures and a
stubbed ``File`` model / ``getApiRoot`` (no live Girder — same spirit as
``test_task_scoping``). The real ACL re-check (403) and the end-to-end submit
route live in ``test_input_resolution_routes`` (server fixture).
"""

import json

import jsonschema
import pytest
from bson.objectid import ObjectId

from girder.exceptions import AccessException, RestException

import contract_loader
from girder_volview.facade import inputs, processing


API_ROOT = "api/v1"


def _mint(fileId, name="slice.dcm", apiRoot=API_ROOT):
    """Build a facade-shaped proxiable uri (mirrors utils.makeFileDownloadUrl)."""
    return f"/{apiRoot}/file/{fileId}/proxiable/{name}"


class _AcceptAllFile:
    """File model stub whose ``load`` returns a doc for any id (ACL always OK)."""

    def load(self, fileId, user=None, level=None, exc=False):
        return {"_id": fileId}


class _DenyFile:
    """File model stub that raises AccessException for a denied id set."""

    def __init__(self, deny):
        self._deny = {str(d) for d in deny}

    def load(self, fileId, user=None, level=None, exc=False):
        if str(fileId) in self._deny:
            raise AccessException("read access denied")
        return {"_id": fileId}


@pytest.fixture(autouse=True)
def _fixed_api_root(monkeypatch):
    # Deterministic mount so the fixtures' ``/api/v1/...`` uris parse regardless
    # of ambient server config; one test flexes a non-default root explicitly.
    monkeypatch.setattr(inputs, "getApiRoot", lambda: API_ROOT)


def _acceptAll(monkeypatch):
    monkeypatch.setattr(inputs, "File", lambda: _AcceptAllFile())


# ---------------------------------------------------------------------------
# _fileIdFromMintedUri — strict own-scheme validation (fail closed)
# ---------------------------------------------------------------------------

def test_recovers_id_from_own_scheme_uri():
    fid = str(ObjectId())
    assert processing._fileIdFromMintedUri(_mint(fid)) == fid
    # a compound-extension name is still a single trailing segment
    assert processing._fileIdFromMintedUri(_mint(fid, "scan.nii.gz")) == fid


@pytest.mark.parametrize("uri", [
    None,
    123,
    "",
    "file/%s/proxiable/x" % ObjectId(),                     # not origin-relative
    "https://evil.example/api/v1/file/%s/proxiable/x" % ObjectId(),  # foreign host
    "/api/v1/item/%s/download" % ObjectId(),                # wrong resource
    "/api/v1/file/%s/download/x" % ObjectId(),              # wrong verb (not proxiable)
    "/api/v1/file/notanobjectid/proxiable/x",               # non-id
    "/api/v1/file/%s/proxiable/" % ObjectId(),              # empty name
    "/api/v1/file/%s/proxiable/a/b" % ObjectId(),           # name has a slash
    "/api/v1/file/%s/proxiable/x?y=1" % ObjectId(),         # query string
    "/api/v1/file/%s/proxiable/x#frag" % ObjectId(),        # fragment
    "/api/v1/file//proxiable/x",                            # missing id
])
def test_rejects_non_own_scheme_uri(uri):
    assert processing._fileIdFromMintedUri(uri) is None


def test_parses_against_configured_api_root_not_a_literal(monkeypatch):
    # Reconciliation flag: recover the id against getApiRoot() (how the minter
    # builds the uri), not a hardcoded /api/v1 — a non-default mount resolves and
    # the default-root shape no longer matches.
    monkeypatch.setattr(inputs, "getApiRoot", lambda: "girder/api/v1")
    fid = str(ObjectId())
    assert processing._fileIdFromMintedUri(
        f"/girder/api/v1/file/{fid}/proxiable/x.nrrd"
    ) == fid
    assert processing._fileIdFromMintedUri(_mint(fid, apiRoot="api/v1")) is None


# ---------------------------------------------------------------------------
# Seam-1 input-value wire conformance (Chunk 29): the golden input-value fixtures
# are a validating consumer of the generated ``input-value.schema.json`` — the
# facade-side stand-in for the normative zod ``inputValueSchema`` (one normative
# definition, two validators; D4). Before Chunk 29 these fixtures were loaded as
# DATA ONLY; now every published schema has a validating consumer. ``jsonschema``
# is a hard test dep (Chunk 29): a missing validator FAILS, never silently skips.
# ---------------------------------------------------------------------------

_INPUT_VALUE_FIXTURES = (
    "wire/input-value.dicom-series.json",
    "wire/input-value.single-file.json",
    "wire/input-value.labelmap.json",
)


def _input_value_validator():
    schema = contract_loader.load_generated_schema("input-value")
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize("fixture_path", _INPUT_VALUE_FIXTURES)
def test_input_value_fixture_validates_against_generated_schema(fixture_path):
    value = contract_loader.load_fixture(fixture_path)
    _input_value_validator().validate(value)  # raises on drift


def test_input_value_missing_uris_is_rejected():
    # ``uris`` is REQUIRED — a value that names a type but carries no bytes handle
    # is not a valid input value (fail closed).
    validator = _input_value_validator()
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"type": "image"})


def test_input_value_rejects_unknown_member():
    # ``additionalProperties: false`` — a stray member is a drift signal, rejected.
    validator = _input_value_validator()
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            {"type": "image", "uris": ["/api/v1/file/x/proxiable/y"], "role": "base"}
        )


# ---------------------------------------------------------------------------
# Translate the Chunk-5 golden input-value fixtures → forwarded file ids (b3)
# ---------------------------------------------------------------------------

def test_dicom_series_fixture_forwards_comma_joined_ids(monkeypatch):
    _acceptAll(monkeypatch)
    value = contract_loader.load_fixture("wire/input-value.dicom-series.json")
    assert len(value["uris"]) > 1  # a real multi-uri series
    params = processing._translateValuesToSlicerParams(
        {"inputVolume": value}, user=object(), folder={"_id": ObjectId()}
    )
    assert params == {
        "inputVolume": (
            "6600000000000000000000a1,"
            "6600000000000000000000a2,"
            "6600000000000000000000a3"
        )
    }


def test_single_file_fixture_forwards_one_id(monkeypatch):
    _acceptAll(monkeypatch)
    value = contract_loader.load_fixture("wire/input-value.single-file.json")
    params = processing._translateValuesToSlicerParams(
        {"inputVolume": value}, user=object(), folder={"_id": ObjectId()}
    )
    assert params == {"inputVolume": "6600000000000000000000b1"}


def test_labelmap_fixture_resolves_through_the_same_path(monkeypatch):
    # Type-agnostic: a staged labelmap resolves like every other input.
    _acceptAll(monkeypatch)
    value = contract_loader.load_fixture("wire/input-value.labelmap.json")
    assert value["type"] == "labelmap"
    params = processing._translateValuesToSlicerParams(
        {"segmentation": value}, user=object(), folder={"_id": ObjectId()}
    )
    assert params == {"segmentation": "6600000000000000000000c1"}


# ---------------------------------------------------------------------------
# Fail-closed submit paths
# ---------------------------------------------------------------------------

def test_foreign_uri_in_value_rejected_400(monkeypatch):
    _acceptAll(monkeypatch)
    value = {
        "type": "image",
        "uris": ["https://evil.example/api/v1/file/%s/proxiable/x" % ObjectId()],
    }
    with pytest.raises(RestException) as exc:
        processing._translateValuesToSlicerParams(
            {"in": value}, user=object(), folder={"_id": ObjectId()}
        )
    assert exc.value.code == 400


def test_one_foreign_uri_fails_the_whole_value_400(monkeypatch):
    _acceptAll(monkeypatch)
    value = {
        "type": "image",
        "uris": [_mint(str(ObjectId())), "/api/v1/item/%s/download" % ObjectId()],
    }
    with pytest.raises(RestException) as exc:
        processing._translateValuesToSlicerParams(
            {"in": value}, user=object(), folder={"_id": ObjectId()}
        )
    assert exc.value.code == 400


def test_empty_uris_rejected_400(monkeypatch):
    _acceptAll(monkeypatch)
    for value in ({"type": "image", "uris": []}, {"type": "image", "uris": "x"}):
        with pytest.raises(RestException) as exc:
            processing._translateValuesToSlicerParams(
                {"in": value}, user=object(), folder={"_id": ObjectId()}
            )
        assert exc.value.code == 400


def test_unreadable_id_raises_access_exception(monkeypatch):
    denied = str(ObjectId())
    monkeypatch.setattr(inputs, "File", lambda: _DenyFile([denied]))
    value = {"type": "image", "uris": [_mint(denied)]}
    with pytest.raises(AccessException):
        processing._translateValuesToSlicerParams(
            {"in": value}, user=object(), folder={"_id": ObjectId()}
        )


def test_scheme_validation_precedes_acl(monkeypatch):
    # A malformed uri fails 400 before any id is loaded, even when another id in
    # the value would be denied — validation is a full pass ahead of authorization.
    denied = str(ObjectId())
    monkeypatch.setattr(inputs, "File", lambda: _DenyFile([denied]))
    value = {
        "type": "image",
        "uris": ["/api/v1/file/notanid/proxiable/x", _mint(denied)],
    }
    with pytest.raises(RestException) as exc:  # RestException(400), not AccessException
        processing._translateValuesToSlicerParams(
            {"in": value}, user=object(), folder={"_id": ObjectId()}
        )
    assert exc.value.code == 400


# ---------------------------------------------------------------------------
# Non-input values still translate; output naming reads the new input shape
# ---------------------------------------------------------------------------

def test_scalars_and_outputs_translate():
    folder = {"_id": ObjectId()}
    params = processing._translateValuesToSlicerParams(
        {
            "threshold": 42,
            "enabled": True,
            "ratio": 0.5,
            "method": "otsu",
            "bounds": [1, 2, 3],
            "outputVolume": {"name": "out.nii.gz"},
        },
        user=None, folder=folder,
    )
    assert params["threshold"] == "42"
    assert params["enabled"] == "true"
    assert params["ratio"] == "0.5"
    assert params["method"] == "otsu"
    assert params["bounds"] == "1,2,3"
    assert params["outputVolume"] == "out.nii.gz"
    assert params["outputVolume_folder"] == str(folder["_id"])


def test_first_input_base_name_derives_from_input_uri():
    value = {"type": "image", "uris": [_mint(str(ObjectId()), "scan.nii.gz")]}
    assert processing._firstInputBaseName({"in": value}) == "scan"
    # No bindable input present → fall back to a generic base.
    assert processing._firstInputBaseName({"threshold": 5}) == "output"
    assert processing._firstInputBaseName({}) == "output"


# ---------------------------------------------------------------------------
# WI3/WI4 — grouping + b1 assembly machinery and its advertisement are gone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol", [
    "_loadedSourcesForFolder",
    "_groupEntriesIntoVolumes",
    "resolveSeriesSourceRefToFiles",
    "decodeSeriesSourceRef",
    "encodeSourceRef",
    "_SERIES_REF_PREFIX",
    "_folderFileEntries",
    "_sliceSortKey",
    "_seriesSource",
    "_singleFileSource",
    "_looksLikeSourceRef",
    "resolveSourceRefToFile",
    "_stageAssembledVolume",
    "_assembleDicomToFile",
    "_parseCliInputs",
    "_taskBindsSingleFile",
    "_resolveSeriesValueToFileId",
    "_createCliJob",
    "_firstSourceRefFile",
])
def test_grouping_and_assembly_symbols_removed(symbol):
    # NB: _cleanupTransientOnJobDone / _markJobTransients / _removeTransientItems
    # were deleted in Chunk 9 alongside the grouping machinery, but Chunk 14
    # REBUILDS that transient-cleanup cluster for staged inputs, so they are
    # intentionally present again and no longer belong on this removed list.
    assert not hasattr(processing, symbol), symbol


def test_provider_config_no_longer_advertises_loaded_sources():
    cfg = processing.buildProcessingConfigBlock({"_id": ObjectId()}, user=None)
    provider = cfg["providers"][0]
    assert provider["context"] == {}
    serialized = json.dumps(cfg)
    assert "loadedSources" not in serialized
    assert "activeSourceRef" not in serialized


# ---------------------------------------------------------------------------
# Chunk 21 item (b): submit-boundary reserved-param deny-list (offline half).
# The end-to-end 400 lives in test_input_resolution_routes; here the pure screen.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reservedKey", ["girderApiUrl", "girderToken"])
def test_reject_reserved_credential_param(reservedKey):
    with pytest.raises(RestException) as exc:
        processing._rejectReservedSubmitParams({reservedKey: "x"})
    assert exc.value.code == 400


@pytest.mark.parametrize("folderKey", ["outputVolume_folder", "_folder", "x_folder"])
def test_reject_undeclared_output_folder_param(folderKey):
    with pytest.raises(RestException) as exc:
        processing._rejectReservedSubmitParams({folderKey: "someid"})
    assert exc.value.code == 400


@pytest.mark.parametrize("values", [
    None,
    {},
    {"inputVolume": {"type": "image", "uris": ["/api/v1/file/x/proxiable/y"]}},
    {"outputVolume": {"name": "result.nrrd"}},  # output request key does not end _folder
    {"threshold": 5, "smoothing": True, "label": "foo"},
])
def test_accepts_well_formed_submissions(values):
    # A compliant submission (the shapes the client actually sends) is untouched.
    assert processing._rejectReservedSubmitParams(values) is None
