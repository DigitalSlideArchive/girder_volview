"""Input values, backend half: the backend resolves its OWN minted URI scheme
back to Girder file ids and forwards them to the CLI as a ``<string>`` param.
These offline unit tests drive the pure resolver + the values→params translation
with the golden ``input-value`` fixtures and a stubbed ``File`` model /
``getApiRoot`` (no live Girder). The real ACL re-check (403) and the end-to-end
submit route live in ``test_input_resolution_routes`` (server fixture).
"""

import json

import jsonschema
import pytest
from bson.objectid import ObjectId

from girder.exceptions import AccessException, RestException

from conftest import API_ROOT
import contract_loader
from girder_volview import handles, utils
from girder_volview.backend import config, inputs, outputs, results, routes, submit


def _mint(fileId, name="slice.dcm", apiRoot=API_ROOT):
    """Build a backend-shaped proxiable uri (mirrors utils.makeFileDownloadUrl)."""
    return f"/{apiRoot}/file/{fileId}/proxiable/{name}"


def _query_ids(query):
    return ((query or {}).get("_id") or {}).get("$in", [])


class _AcceptAllFile:
    """File model stub for the batched resolver: ``find`` returns a doc for every
    requested id (each its own parent item), so the ACL always passes."""

    def find(self, query=None, **kwargs):
        # itemId == fileId keeps the parent-item ACL model 1:1 with the file.
        return [{"_id": i, "itemId": i} for i in _query_ids(query)]


class _AcceptAllItem:
    """Item model stub: every requested parent item is READ-able."""

    def findWithPermissions(self, query=None, user=None, level=None, **kwargs):
        return [{"_id": i} for i in _query_ids(query)]


class _DenyFile:
    """File model stub for the batched resolver: every id resolves to a file whose
    parent item is itself (ACL lives on the item, so ``_DenyItem`` does denial)."""

    def find(self, query=None, **kwargs):
        return [{"_id": i, "itemId": i} for i in _query_ids(query)]


class _DenyItem:
    """Item model stub whose ``findWithPermissions`` drops a denied id set, so the
    batched resolver sees the file's parent as unreadable and raises 403."""

    def __init__(self, deny):
        self._deny = {str(d) for d in deny}

    def findWithPermissions(self, query=None, user=None, level=None, **kwargs):
        return [{"_id": i} for i in _query_ids(query) if str(i) not in self._deny]


@pytest.fixture(autouse=True)
def _fixed_api_root(_fixed_api_root, monkeypatch):
    # Deterministic mount so the fixtures' ``/api/v1/...`` uris parse regardless
    # of ambient server config; one test flexes a non-default root explicitly.
    # The shared conftest pin covers ``handles`` (the mint/parse pair's defining
    # module); ``config`` is patched too because the provider config's
    # ``baseUrl``/``jobsBaseUrl`` derive from ``config.getApiRoot()``.
    monkeypatch.setattr(config, "getApiRoot", lambda: API_ROOT)


def _acceptAll(monkeypatch):
    monkeypatch.setattr(inputs, "File", lambda: _AcceptAllFile())
    monkeypatch.setattr(inputs, "Item", lambda: _AcceptAllItem())


def test_recovers_id_from_own_scheme_uri():
    fid = str(ObjectId())
    assert inputs._fileIdFromMintedUri(_mint(fid)) == fid
    # a compound-extension name is still a single trailing segment
    assert inputs._fileIdFromMintedUri(_mint(fid, "scan.nii.gz")) == fid


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
        # foreign host
        "https://evil.example/api/v1/file/%s/proxiable/x" % _PLACEHOLDER_ID,
        "/api/v1/item/%s/download" % _PLACEHOLDER_ID,  # wrong resource
        "/api/v1/file/%s/download/x" % _PLACEHOLDER_ID,  # wrong verb (not proxiable)
        "/api/v1/file/notanobjectid/proxiable/x",  # non-id
        "/api/v1/file/%s/proxiable/" % _PLACEHOLDER_ID,  # empty name
        "/api/v1/file/%s/proxiable/a/b" % _PLACEHOLDER_ID,  # name has a slash
        "/api/v1/file//proxiable/x",  # missing id
    ],
)
def test_rejects_non_own_scheme_uri(uri):
    assert inputs._fileIdFromMintedUri(uri) is None


@pytest.mark.parametrize(
    "name",
    [
        "Lesion #1.seg.nrrd",  # fragment delimiter in a legal Girder name
        "flow ?phase.nrrd",  # query delimiter (submit-400 scenario)
        "coverage 50%.seg.nrrd",
    ],
)
def test_accepts_the_backends_own_mint_for_reserved_char_names(name):
    # Mint and parse are ONE module, so the parser accepts what the minter emits
    # for every legal Girder file name -- escaped or raw.
    fid = str(ObjectId())
    minted = utils.makeFileDownloadUrl({"_id": fid, "name": name})
    assert inputs._fileIdFromMintedUri(minted) == fid
    assert inputs._fileIdFromMintedUri(_mint(fid, name)) == fid  # legacy raw


def test_parses_against_configured_api_root_not_a_literal(monkeypatch):
    # The id is recovered against getApiRoot() (how the minter builds the uri),
    # not a hardcoded /api/v1, so a non-default mount resolves.
    monkeypatch.setattr(handles, "getApiRoot", lambda: "girder/api/v1")
    fid = str(ObjectId())
    assert (
        inputs._fileIdFromMintedUri(f"/girder/api/v1/file/{fid}/proxiable/x.nrrd")
        == fid
    )
    assert inputs._fileIdFromMintedUri(_mint(fid, apiRoot="api/v1")) is None


# The golden input-value fixtures are a validating consumer of the generated
# ``input-value.schema.json`` — the backend-side stand-in for the normative zod
# ``inputValueSchema`` (one normative definition, two validators). ``jsonschema``
# is a hard test dep: a missing validator FAILS, never silently skips.
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


def test_dicom_series_fixture_forwards_comma_joined_ids(monkeypatch):
    _acceptAll(monkeypatch)
    value = contract_loader.load_fixture("wire/input-value.dicom-series.json")
    assert len(value["uris"]) > 1  # a real multi-uri series
    params, _ = submit._translateValuesToSlicerParams(
        {"inputVolume": value}, user=object(), outputFolder={"_id": ObjectId()}
    )
    assert params == {
        "inputVolume": (
            "6600000000000000000000a1,6600000000000000000000a2,6600000000000000000000a3"
        )
    }


def test_single_file_fixture_forwards_one_id(monkeypatch):
    _acceptAll(monkeypatch)
    value = contract_loader.load_fixture("wire/input-value.single-file.json")
    params, _ = submit._translateValuesToSlicerParams(
        {"inputVolume": value}, user=object(), outputFolder={"_id": ObjectId()}
    )
    assert params == {"inputVolume": "6600000000000000000000b1"}


def test_labelmap_fixture_resolves_through_the_same_path(monkeypatch):
    # Type-agnostic: a staged labelmap resolves like every other input.
    _acceptAll(monkeypatch)
    value = contract_loader.load_fixture("wire/input-value.labelmap.json")
    assert value["type"] == "labelmap"
    params, _ = submit._translateValuesToSlicerParams(
        {"segmentation": value}, user=object(), outputFolder={"_id": ObjectId()}
    )
    assert params == {"segmentation": "6600000000000000000000c1"}


def test_submit_reuses_resolved_files_for_params_and_transient_detection(monkeypatch):
    file_ids = [str(ObjectId()), str(ObjectId()), str(ObjectId())]
    parent_id = ObjectId()
    counts = {"fileFind": 0, "itemPermFind": 0, "itemFind": 0}

    class Files:
        def find(self, query=None, **kwargs):
            counts["fileFind"] += 1
            return [{"_id": i, "itemId": parent_id} for i in _query_ids(query)]

    class Items:
        def findWithPermissions(self, query=None, user=None, level=None, **kwargs):
            counts["itemPermFind"] += 1
            return [{"_id": i} for i in _query_ids(query)]

        def find(self, query=None, **kwargs):
            counts["itemFind"] += 1
            return [{"_id": i, "meta": {}} for i in _query_ids(query)]

    monkeypatch.setattr(inputs, "File", Files)
    monkeypatch.setattr(inputs, "Item", Items)
    values = {
        "inputVolume": {
            "type": "image",
            "format": "dicom-series",
            "uris": [_mint(file_id) for file_id in file_ids],
        }
    }

    params, resolved = submit._translateValuesToSlicerParams(
        values,
        user=object(),
        outputFolder={"_id": ObjectId()},
    )
    params, copied = inputs.copyStagedInputsIntoJobFolder(
        params, resolved, user=object(), outputFolder={"_id": ObjectId()}
    )

    # Durable inputs: nothing is copied and the params pass through untouched.
    assert params == {"inputVolume": ",".join(file_ids)}
    assert copied == []
    # The batched resolver does one file find + one permission-filtered parent-item
    # find (never a per-uri load); the copy pass reads the transient markers with
    # ONE batched find over the distinct parents, reusing the already-resolved docs.
    assert counts == {"fileFind": 1, "itemPermFind": 1, "itemFind": 1}


def test_integral_float_translates_to_canonical_int_form():
    # Validation accepts 5.0 for an <integer> param (JSON has no int/float
    # split), so translation must emit "5", not "5.0" — the CLI's argparse
    # int()/enum parsing rejects the float string. Same per vector element;
    # fractionals pass through.
    params, _ = submit._translateValuesToSlicerParams(
        {"iterations": 5.0, "sigma": 2.5, "radii": [5.0, 2.5]},
        user=object(),
        outputFolder={"_id": ObjectId()},
    )
    assert params == {"iterations": "5", "sigma": "2.5", "radii": "5,2.5"}


def test_reserved_char_named_input_translates_without_400(monkeypatch):
    # A '#'-named file's own backend-minted handle survives the whole submit
    # translation.
    _acceptAll(monkeypatch)
    fid = str(ObjectId())
    value = {
        "type": "image",
        "uris": [utils.makeFileDownloadUrl({"_id": fid, "name": "scan #2.nrrd"})],
    }
    params, _ = submit._translateValuesToSlicerParams(
        {"inputVolume": value}, user=object(), outputFolder={"_id": ObjectId()}
    )
    assert params == {"inputVolume": fid}


def test_foreign_uri_in_value_rejected_400(monkeypatch):
    _acceptAll(monkeypatch)
    value = {
        "type": "image",
        "uris": ["https://evil.example/api/v1/file/%s/proxiable/x" % ObjectId()],
    }
    with pytest.raises(RestException) as exc:
        submit._translateValuesToSlicerParams(
            {"in": value}, user=object(), outputFolder={"_id": ObjectId()}
        )
    assert exc.value.code == 400


def test_one_foreign_uri_fails_the_whole_value_400(monkeypatch):
    _acceptAll(monkeypatch)
    value = {
        "type": "image",
        "uris": [_mint(str(ObjectId())), "/api/v1/item/%s/download" % ObjectId()],
    }
    with pytest.raises(RestException) as exc:
        submit._translateValuesToSlicerParams(
            {"in": value}, user=object(), outputFolder={"_id": ObjectId()}
        )
    assert exc.value.code == 400


def test_empty_uris_rejected_400(monkeypatch):
    _acceptAll(monkeypatch)
    for value in ({"type": "image", "uris": []}, {"type": "image", "uris": "x"}):
        with pytest.raises(RestException) as exc:
            submit._translateValuesToSlicerParams(
                {"in": value}, user=object(), outputFolder={"_id": ObjectId()}
            )
        assert exc.value.code == 400


def test_unreadable_id_raises_access_exception(monkeypatch):
    denied = str(ObjectId())
    monkeypatch.setattr(inputs, "File", lambda: _DenyFile())
    monkeypatch.setattr(inputs, "Item", lambda: _DenyItem([denied]))
    value = {"type": "image", "uris": [_mint(denied)]}
    with pytest.raises(AccessException):
        submit._translateValuesToSlicerParams(
            {"in": value}, user=object(), outputFolder={"_id": ObjectId()}
        )


def test_scheme_validation_precedes_acl(monkeypatch):
    # A malformed uri fails 400 before any id is loaded, even when another id in
    # the value would be denied — validation is a full pass ahead of authorization.
    denied = str(ObjectId())
    monkeypatch.setattr(inputs, "File", lambda: _DenyFile())
    monkeypatch.setattr(inputs, "Item", lambda: _DenyItem([denied]))
    value = {
        "type": "image",
        "uris": ["/api/v1/file/notanid/proxiable/x", _mint(denied)],
    }
    with pytest.raises(RestException) as exc:  # RestException(400), not AccessException
        submit._translateValuesToSlicerParams(
            {"in": value}, user=object(), outputFolder={"_id": ObjectId()}
        )
    assert exc.value.code == 400


def test_scalars_and_outputs_translate():
    # Output location is SERVER-OWNED: every declared output is forced into the
    # job's private output folder, passed in as ``outputFolder``.
    outputFolder = {"_id": ObjectId()}
    params, _ = submit._translateValuesToSlicerParams(
        {
            "threshold": 42,
            "enabled": True,
            "ratio": 0.5,
            "method": "otsu",
            "bounds": [1, 2, 3],
            "outputVolume": {"name": "out.nii.gz"},
        },
        user=None,
        outputFolder=outputFolder,
    )
    assert params["threshold"] == "42"
    assert params["enabled"] == "true"
    assert params["ratio"] == "0.5"
    assert params["method"] == "otsu"
    assert params["bounds"] == "1,2,3"
    assert params["outputVolume"] == "out.nii.gz"
    assert params["outputVolume_folder"] == str(outputFolder["_id"])


def test_annotation_output_forced_into_private_output_folder():
    # A labelmap output is forced into the job's private output folder (the sole
    # correlation + ownership key), not the launch folder.
    outputFolder = {"_id": ObjectId()}
    params, _ = submit._translateValuesToSlicerParams(
        {"outputVolume": {"name": "labels.seg.nrrd"}},
        user="owner",
        outputFolder=outputFolder,
    )

    assert params["outputVolume_folder"] == str(outputFolder["_id"])


def test_report_output_forced_into_private_output_folder():
    # A report (file) output is forced into the same private output folder as every
    # other declared output -- the destination is not per-output-typed.
    outputFolder = {"_id": ObjectId()}
    params, _ = submit._translateValuesToSlicerParams(
        {"report": {"name": "measurements.csv"}},
        user="owner",
        outputFolder=outputFolder,
    )

    assert params["report_folder"] == str(outputFolder["_id"])


def test_submitted_output_folder_ref_is_rejected_400():
    # A client-supplied folderRef on an output value would redirect a job's
    # outputs out of its own (correlation-key) folder, so it is REJECTED --
    # neither honored nor silently stripped.
    outputFolder = {"_id": ObjectId()}
    with pytest.raises(RestException) as exc:
        submit._translateValuesToSlicerParams(
            {"outputVolume": {"name": "labels.seg.nrrd", "folderRef": "chosen"}},
            user="owner",
            outputFolder=outputFolder,
        )
    assert exc.value.code == 400


def test_first_input_base_name_derives_from_input_uri():
    value = {"type": "image", "uris": [_mint(str(ObjectId()), "scan.nii.gz")]}
    assert submit._firstInputBaseName({"in": value}) == "scan"
    assert submit._firstInputBaseName({"threshold": 5}) == "output"
    assert submit._firstInputBaseName({}) == "output"


@pytest.mark.parametrize(
    "symbol",
    [
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
    ],
)
def test_grouping_and_assembly_symbols_removed(symbol):
    # There is no single successor to the former ``processing`` monolith, so the
    # symbol must be absent from EVERY surviving backend module.
    for module in (inputs, submit, outputs, results, routes, config):
        assert not hasattr(module, symbol), (module.__name__, symbol)


def test_provider_config_no_longer_advertises_loaded_sources():
    cfg = config.buildProcessingConfigBlock({"_id": ObjectId()})
    provider = cfg["providers"][0]
    assert provider["context"] == {}
    serialized = json.dumps(cfg)
    assert "loadedSources" not in serialized
    assert "activeSourceRef" not in serialized


def test_provider_config_advertises_explicit_jobs_base_url():
    # The config block advertises the explicit folder-free root for the
    # job-addressed routes (status/results/cancel) alongside the folder-scoped
    # baseUrl, so the client never string-surgeries the folder segment out of
    # baseUrl. It matches the _JobResource mount (routes.py), a sibling of
    # /folder.
    folderId = ObjectId()
    provider = config.buildProcessingConfigBlock({"_id": folderId})[
        "providers"
    ][0]
    assert provider["baseUrl"] == ("/api/v1/folder/%s/volview_processing" % folderId)
    assert provider["jobsBaseUrl"] == "/api/v1/volview_processing"
    # Folder-free: no launch folder id leaks into the jobs base.
    assert str(folderId) not in provider["jobsBaseUrl"]
    # The provider id is FOLDER-SCOPED (immutable per launch folder). This bare
    # dict has no name, so the label falls back to the plain "Analysis".
    assert provider["id"] == ("girder-slicer-cli:%s" % folderId)
    assert provider["label"] == "Analysis"


def test_provider_config_id_is_folder_scoped_and_label_carries_folder_name():
    # Two folders open simultaneously must register as two DISTINCT providers, so
    # the id carries the launch folder id and the label carries the folder name
    # (the picker distinguishes them). A folder dict with no name falls back to
    # the bare "Analysis" label.
    folderId = ObjectId()
    named = config.buildProcessingConfigBlock(
        {"_id": folderId, "name": "Chest CT"}
    )["providers"][0]
    assert named["id"] == ("girder-slicer-cli:%s" % folderId)
    assert named["label"] == "Analysis — Chest CT"

    nameless = config.buildProcessingConfigBlock({"_id": folderId})[
        "providers"
    ][0]
    assert nameless["id"] == ("girder-slicer-cli:%s" % folderId)
    assert nameless["label"] == "Analysis"


def test_provider_config_urls_derive_from_api_root_not_a_literal(monkeypatch):
    # baseUrl/jobsBaseUrl are built from getApiRoot() (like file download urls),
    # not a hardcoded /api/v1, so a non-default mount resolves instead of 404ing
    # every submit/status/results call. Only a non-default root proves this --
    # the default-root assertion above would pass against a literal too.
    monkeypatch.setattr(config, "getApiRoot", lambda: "girder/api/v1")
    folderId = ObjectId()
    provider = config.buildProcessingConfigBlock({"_id": folderId})[
        "providers"
    ][0]
    assert provider["baseUrl"] == (
        "/girder/api/v1/folder/%s/volview_processing" % folderId
    )
    assert provider["jobsBaseUrl"] == "/girder/api/v1/volview_processing"


# Offline half of the submit-boundary reserved-param deny-list; the end-to-end
# 400 lives in test_input_resolution_routes.
@pytest.mark.parametrize("reservedKey", ["girderApiUrl", "girderToken"])
def test_reject_reserved_credential_param(reservedKey):
    with pytest.raises(RestException) as exc:
        submit._rejectReservedSubmitParams({reservedKey: "x"})
    assert exc.value.code == 400


def test_reject_synthesized_output_folder_collision():
    # The backend synthesizes outputVolume_folder for the declared output; a
    # raw submission of that key would redirect where the output is written.
    declared = {"outputVolume": {"channel": "output", "tag": "image"}}
    with pytest.raises(RestException) as exc:
        submit._rejectSynthesizedFolderParams(
            {"outputVolume_folder": "someid"}, declared
        )
    assert exc.value.code == 400


@pytest.mark.parametrize("folderKey", ["_folder", "x_folder"])
def test_undeclared_folder_suffix_param_still_rejected(folderKey):
    # A *_folder key that collides with nothing synthesized is not reserved,
    # but it is undeclared — the undeclared screen still 400s it.
    declared = {"outputVolume": {"channel": "output", "tag": "image"}}
    assert (
        submit._rejectSynthesizedFolderParams({folderKey: "someid"}, declared) is None
    )
    with pytest.raises(RestException) as exc:
        submit._rejectUndeclaredSubmitParams({folderKey: "someid"}, declared)
    assert exc.value.code == 400


def test_declared_folder_suffix_param_is_submittable():
    # A CLI may legitimately declare its own *_folder param (e.g. a <directory>
    # input named atlas_folder); only synthesized names are reserved.
    declared = {
        "atlas_folder": {"channel": "input", "tag": "directory"},
        "outputVolume": {"channel": "output", "tag": "image"},
    }
    values = {"atlas_folder": "some-directory"}
    assert submit._rejectSynthesizedFolderParams(values, declared) is None
    assert submit._rejectUndeclaredSubmitParams(values, declared) is None


@pytest.mark.parametrize(
    "values",
    [
        None,
        {},
        {"inputVolume": {"type": "image", "uris": ["/api/v1/file/x/proxiable/y"]}},
        {
            "outputVolume": {"name": "result.nrrd"}
        },  # output request key does not end _folder
        {"threshold": 5, "smoothing": True, "label": "foo"},
    ],
)
def test_accepts_well_formed_submissions(values):
    assert submit._rejectReservedSubmitParams(values) is None
