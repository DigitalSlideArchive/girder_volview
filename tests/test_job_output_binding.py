"""Offline unit coverage for folder-owned job outputs.

Covers the binding rules stated in ``backend/outputs.py``: correlation by the
finalized file's actual private parent folder, declared identifiers only, and
``getJobResults`` failing loud on a non-succeeded job with a ``missing`` count
rather than a silent ``[]``.

These drive the pure control flow with fake Girder/Job/Item/File models, so they
need no live Girder. The real Mongo-backed lifecycle (dotted ``$set`` nesting,
the item->folder hop against real docs, private-folder ACL, the results route,
two jobs against two folders) lives in ``test_job_output_binding_routes``.
"""

import json
from conftest import _Event

import pytest
from bson.objectid import ObjectId

from girder_jobs.constants import JobStatus

from girder_volview.backend import outputs as outputs_mod
from girder_volview.backend import inputs, results as results_mod, routes, slicer_spec

_OUTPUTS = outputs_mod._OUTPUTS_FIELD
_SPECS = outputs_mod._OUTPUT_SPECS_FIELD
_FOLDER = outputs_mod._OUTPUT_FOLDER_ID_FIELD


class _FakeJob:
    """Job() stand-in: ``findOne`` by output-folder id, ``updateJob`` records the
    otherFields it was called with AND applies dotted keys into a nested map so a
    follow-up read sees them (mirrors how Mongo interprets a dotted ``$set``
    path). Correlation is keyed by output-folder id; there is no token."""

    def __init__(self, jobs=None):
        self._byId = {str(j["_id"]): j for j in (jobs or [])}
        self.updated = []

    def load(self, jobId, force=False, exc=False, user=None, level=None):
        return self._byId.get(str(jobId))

    def findOne(self, query):
        for job in self._byId.values():
            if all(job.get(key) == want for key, want in query.items()):
                return job
        return None

    def updateJob(self, job, otherFields=None):
        self.updated.append(otherFields or {})
        for key, value in (otherFields or {}).items():
            if "." in key:
                head, tail = key.split(".", 1)
                job.setdefault(head, {})[tail] = value
            else:
                job[key] = value
        return job

    def exposeFields(self, level=None, fields=None):
        pass


def _installJob(monkeypatch, model):
    import girder_jobs.models.job as job_module

    monkeypatch.setattr(job_module, "Job", lambda: model)
    return model


class _FakeItem:
    """Item() stand-in: ``load`` maps an item id to a doc carrying ``folderId``.

    The item->folder hop is essential -- a File doc has no ``folderId`` -- so the
    correlation key is the item's parent folder, exactly as in real Girder."""

    def __init__(self, byId=None):
        self._byId = byId or {}

    def load(self, itemId, force=False, exc=False, user=None, level=None):
        return self._byId.get(str(itemId))


def _installItem(monkeypatch, byId=None):
    monkeypatch.setattr(outputs_mod, "Item", lambda: _FakeItem(byId))


class _FakeFile:
    """File() stand-in for the batched loader: ``find`` returns the canned docs
    matching the id query (an absent id models a deleted file)."""

    def __init__(self, byId=None):
        self._byId = byId or {}

    def find(self, query, fields=None):
        wanted = {str(i) for i in query["_id"]["$in"]}
        return [doc for key, doc in self._byId.items() if key in wanted]


class _FakeReadableItems:
    """Item() stand-in for the batched loader: every parent item is readable."""

    def findWithPermissions(self, query, fields=None, user=None, level=None):
        return [{"_id": itemId} for itemId in query["_id"]["$in"]]


def _installFile(monkeypatch, byId=None):
    byId = byId or {}
    for doc in byId.values():
        # The batched loader resolves readability through the parent item; give
        # each canned file a (readable) one unless the test models its own.
        doc.setdefault("itemId", ObjectId())
    # The batched loader lives in inputs.readableFilesById (results delegates).
    monkeypatch.setattr(inputs, "File", lambda: _FakeFile(byId))
    monkeypatch.setattr(inputs, "Item", lambda: _FakeReadableItems())


def _deterministicUrls(monkeypatch):
    # Avoid the getApiRoot/server dependency; the real url shape is covered in the
    # server-fixture suite.
    monkeypatch.setattr(
        results_mod,
        "makeFileDownloadUrl",
        lambda f: "/api/v1/file/%s/proxiable/%s" % (f["_id"], f["name"]),
    )


def _spec(name, tag="image", isLabel=False, ext=""):
    return {"name": name, "tag": tag, "isLabel": isLabel, "fileExtensions": ext}


def _job(outputs, specs, status=JobStatus.SUCCESS):
    return {
        "_id": ObjectId(),
        "status": status,
        inputs._LAUNCH_FOLDER_FIELD: "folder-1",
        _OUTPUTS: outputs,
        _SPECS: specs,
    }


def _folderJob(folderId, specs, outputs=None, status=JobStatus.SUCCESS):
    """A job that OWNS an output folder (correlation + ownership key)."""
    return {
        "_id": ObjectId(),
        "status": status,
        inputs._LAUNCH_FOLDER_FIELD: str(folderId),
        _FOLDER: str(folderId),
        _SPECS: specs,
        _OUTPUTS: outputs if outputs is not None else {},
    }


def _uploadEvent(reference, fileId, itemId="item-1"):
    file_doc = None
    if fileId is not None:
        file_doc = {"_id": fileId, "itemId": itemId}
    return _Event({"upload": {"reference": reference}, "file": file_doc})


def _bindingSetup(monkeypatch, folderId="folder-1", itemId="item-1", specs=None):
    """Install a folder-keyed job + an item->folder map so ``_recordJobOutput`` can
    hop file -> item -> folder -> job. Returns ``(job, model)``."""
    job = _folderJob(folderId, specs if specs is not None else [_spec("outVol")])
    model = _installJob(monkeypatch, _FakeJob([job]))
    _installItem(monkeypatch, {str(itemId): {"_id": itemId, "folderId": folderId}})
    return job, model


def test_parse_reference_accepts_slicer_shaped_ref():
    ref = outputs_mod._parseOutputReference(
        json.dumps({"slicer_cli_web": {}, "identifier": "outVol", "uuid": "u"})
    )
    assert ref["identifier"] == "outVol"


def test_parse_reference_accepts_dict_passthrough():
    assert outputs_mod._parseOutputReference({"identifier": "x"})["identifier"] == "x"


def test_parse_reference_rejects_non_json_or_non_dict():
    assert outputs_mod._parseOutputReference(None) is None
    assert outputs_mod._parseOutputReference("not json") is None
    assert outputs_mod._parseOutputReference(json.dumps(["a", "b"])) is None


def test_parse_reference_requires_a_nonempty_identifier():
    assert outputs_mod._parseOutputReference(json.dumps({"uuid": "u"})) is None
    assert outputs_mod._parseOutputReference(json.dumps({"identifier": ""})) is None


def test_parse_reference_rejects_operator_or_dotted_identifier():
    # A dotted / $-bearing identifier could otherwise forge a nested or operator
    # $set key when recorded onto the job.
    assert outputs_mod._parseOutputReference(json.dumps({"identifier": "a.b"})) is None
    assert outputs_mod._parseOutputReference(json.dumps({"identifier": "$set"})) is None


# _declaredOutputIdentifiers: the set an upload identifier must belong to.


def test_declared_identifiers_reads_the_spec_names():
    job = {_SPECS: [_spec("outVol"), _spec("outSeg", isLabel=True)]}
    assert outputs_mod._declaredOutputIdentifiers(job) == {"outVol", "outSeg"}


def test_declared_identifiers_empty_without_specs():
    assert outputs_mod._declaredOutputIdentifiers({}) == set()
    assert outputs_mod._declaredOutputIdentifiers(None) == set()
    assert outputs_mod._declaredOutputIdentifiers({_SPECS: "not-a-list"}) == set()


def test_job_for_folder_correlates_by_output_folder_id(monkeypatch):
    job = _folderJob("folder-1", [_spec("o")])
    _installJob(monkeypatch, _FakeJob([job]))
    assert outputs_mod._jobForOutputFolder("folder-1") is job


def test_job_for_folder_stringifies_the_query_id(monkeypatch):
    folderId = ObjectId()
    job = _folderJob(folderId, [_spec("o")])  # stored as str(folderId)
    _installJob(monkeypatch, _FakeJob([job]))
    assert outputs_mod._jobForOutputFolder(folderId) is job


def test_job_for_folder_uncorrelated_is_none(monkeypatch):
    _installJob(monkeypatch, _FakeJob([_folderJob("folder-1", [_spec("o")])]))
    assert outputs_mod._jobForOutputFolder("other-folder") is None
    assert outputs_mod._jobForOutputFolder(None) is None


def test_record_output_binds_file_under_identifier(monkeypatch):
    job, model = _bindingSetup(monkeypatch, specs=[_spec("outVol")])
    fid = ObjectId()

    outputs_mod._recordJobOutput(
        _uploadEvent(json.dumps({"identifier": "outVol"}), fid)
    )

    # Recorded via a dotted key so Mongo $set nests per identifier.
    assert model.updated == [{"%s.outVol" % _OUTPUTS: str(fid)}]
    assert job[_OUTPUTS]["outVol"] == str(fid)


def test_record_output_n_outputs_all_bind_without_overwrite(monkeypatch):
    names = ("outA", "outB", "outC")
    job, _model = _bindingSetup(monkeypatch, specs=[_spec(name) for name in names])

    expected = {}
    for name in names:
        fid = ObjectId()
        expected[name] = str(fid)
        outputs_mod._recordJobOutput(
            _uploadEvent(json.dumps({"identifier": name}), fid)
        )

    assert job[_OUTPUTS] == expected


def test_record_output_ignores_referenceless_or_bad_upload(monkeypatch):
    job, model = _bindingSetup(monkeypatch, specs=[_spec("o")])

    outputs_mod._recordJobOutput(_uploadEvent(None, ObjectId()))  # no reference
    outputs_mod._recordJobOutput(_uploadEvent("not json", ObjectId()))  # bad reference
    outputs_mod._recordJobOutput(
        _uploadEvent(json.dumps({"identifier": "o"}), None)
    )  # no file

    assert model.updated == []
    assert job[_OUTPUTS] == {}


def test_record_output_ignores_upload_whose_folder_owns_no_job(monkeypatch):
    # The finalized file's parent folder is not any backend job's output folder,
    # so a foreign or uncorrelated upload is never recorded.
    job = _folderJob("folder-1", [_spec("o")])
    model = _installJob(monkeypatch, _FakeJob([job]))
    _installItem(
        monkeypatch, {"item-1": {"_id": "item-1", "folderId": "orphan-folder"}}
    )

    outputs_mod._recordJobOutput(
        _uploadEvent(json.dumps({"identifier": "o"}), ObjectId())
    )

    assert model.updated == []
    assert job[_OUTPUTS] == {}


def test_record_output_ignores_upload_whose_item_is_gone(monkeypatch):
    job = _folderJob("folder-1", [_spec("o")])
    model = _installJob(monkeypatch, _FakeJob([job]))
    _installItem(monkeypatch, {})  # item load -> None (no parent folder to correlate)

    outputs_mod._recordJobOutput(
        _uploadEvent(json.dumps({"identifier": "o"}), ObjectId())
    )

    assert model.updated == []


def test_record_output_ignores_malformed_event(monkeypatch):
    model = _installJob(monkeypatch, _FakeJob([]))
    _installItem(monkeypatch, {})
    outputs_mod._recordJobOutput(_Event(None))
    outputs_mod._recordJobOutput(_Event("not-a-dict"))
    assert model.updated == []


def test_record_output_binds_only_by_actual_parent_folder(monkeypatch):
    # A crafted reference carrying ANOTHER job's id, a foreign uuid, a token, and
    # an innocent filename still binds ONLY by the finalized file's actual private
    # parent folder. Two jobs, each owning a distinct folder; the file really lives
    # in jobA's folder while the reference names jobB.
    jobA = _folderJob("folderA", [_spec("outVol")])
    jobB = _folderJob("folderB", [_spec("outVol")])
    _installJob(monkeypatch, _FakeJob([jobA, jobB]))
    _installItem(monkeypatch, {"item-1": {"_id": "item-1", "folderId": "folderA"}})
    fid = ObjectId()

    crafted = json.dumps(
        {
            "identifier": "outVol",
            "jobId": str(jobB["_id"]),
            "uuid": "attacker-uuid",
            "token": "attacker-token",
            "filename": "innocent.nii.gz",
        }
    )
    outputs_mod._recordJobOutput(_uploadEvent(crafted, fid, itemId="item-1"))

    # The file bound to jobA (its real parent folder), never the named jobB.
    assert jobA[_OUTPUTS] == {"outVol": str(fid)}
    assert jobB[_OUTPUTS] == {}


def test_record_output_rejects_undeclared_identifier(monkeypatch):
    # An upload into the job's own folder whose identifier the job never declared
    # is refused: correlation binds only DECLARED outputs.
    job, model = _bindingSetup(monkeypatch, specs=[_spec("outVol")])

    outputs_mod._recordJobOutput(
        _uploadEvent(json.dumps({"identifier": "smuggled"}), ObjectId())
    )

    assert model.updated == []
    assert job[_OUTPUTS] == {}


def test_record_output_rejects_unsafe_identifier(monkeypatch):
    # A dotted / operator identifier is dropped by the parse guard before it could
    # ever build a nested / $set key -- even were it declared.
    job, model = _bindingSetup(monkeypatch, specs=[_spec("a.b"), _spec("$set")])

    for ident in ("a.b", "$set"):
        outputs_mod._recordJobOutput(
            _uploadEvent(json.dumps({"identifier": ident}), ObjectId())
        )

    assert model.updated == []
    assert job[_OUTPUTS] == {}


def test_record_output_propagates_matched_binding_failure(monkeypatch):
    job, model = _bindingSetup(monkeypatch, specs=[_spec("outVol")])

    def failUpdate(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(model, "updateJob", failUpdate)
    with pytest.raises(RuntimeError, match="database write failed"):
        outputs_mod._recordJobOutput(
            _uploadEvent(json.dumps({"identifier": "outVol"}), ObjectId())
        )


def test_prepare_submission_fields_records_specs_folder_and_empty_map():
    xml = (
        "<executable><parameters>"
        '<image type="label"><name>outSeg</name><channel>output</channel></image>'
        "</parameters></executable>"
    )
    folder = {"_id": ObjectId()}
    outputFolder = {"_id": ObjectId()}
    fields = routes._prepareSubmissionFields(
        "sub-1",
        folder,
        "task",
        {"threshold": 3},
        slicer_spec.parse_cli(xml)["outputs"],
        [],
        outputFolder,
    )
    assert fields[_OUTPUTS] == {}
    assert fields[_SPECS] == [
        {"name": "outSeg", "tag": "image", "isLabel": True, "fileExtensions": ""}
    ]
    # The private output folder id is part of the FIRST insert -- it is the sole
    # correlation + ownership key.
    assert fields[_FOLDER] == str(outputFolder["_id"])
    assert fields[inputs._LAUNCH_FOLDER_FIELD] == str(folder["_id"])
    assert fields["volviewSubmittedParameters"] == {"threshold": 3}
    assert fields["volviewSubmissionId"] == "sub-1"


def test_status_success_with_unrecorded_declared_output_is_incomplete():
    job = {
        "_id": ObjectId(),
        "status": JobStatus.SUCCESS,
        _SPECS: [_spec("outVol")],
        _OUTPUTS: {},
    }
    status = results_mod._projectJobStatus(job)
    assert status["state"] == "success"
    assert status["resultState"] == "incomplete"


def test_status_success_when_no_outputs_declared():
    job = {"_id": ObjectId(), "status": JobStatus.SUCCESS, _SPECS: [], _OUTPUTS: {}}
    assert results_mod._projectJobStatus(job)["resultState"] == "ready"


def test_status_running_job_waits_for_results():
    job = {
        "_id": ObjectId(),
        "status": JobStatus.RUNNING,
        _SPECS: [_spec("o")],
        _OUTPUTS: {},
    }
    assert results_mod._projectJobStatus(job)["resultState"] == "waiting"


def test_collect_reads_ids_off_the_job(monkeypatch):
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(
        monkeypatch,
        {
            str(fid): {
                "_id": fid,
                "name": "brain.otsu.nii.gz",
                "mimeType": "application/octet-stream",
                "size": 10,
            },
        },
    )
    job = _job({"outVol": str(fid)}, [_spec("outVol")])

    results, missing = results_mod._collectJobResults(job, user=None)

    assert missing == 0 and len(results) == 1
    assert results[0]["intent"] == "add-base-image"
    assert results[0]["url"] == "/api/v1/file/%s/proxiable/brain.otsu.nii.gz" % fid
    assert results[0]["id"] == str(fid)


def test_collect_n_outputs_all_bind(monkeypatch):
    _deterministicUrls(monkeypatch)
    files, outputs, specs = {}, {}, []
    for name in ("outA", "outB", "outC"):
        fid = ObjectId()
        files[str(fid)] = {"_id": fid, "name": name + ".nii.gz"}
        outputs[name] = str(fid)
        specs.append(_spec(name))
    _installFile(monkeypatch, files)

    results, missing = results_mod._collectJobResults(_job(outputs, specs), user=None)

    assert missing == 0 and len(results) == 3


def test_collect_deleted_output_counts_missing(monkeypatch):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})  # load -> None (deleted)

    results, missing = results_mod._collectJobResults(
        _job({"outVol": str(ObjectId())}, [_spec("outVol")]), user=None
    )

    assert results == [] and missing == 1


def test_collect_partial_loss_returns_resolved_and_counts_missing(monkeypatch):
    _deterministicUrls(monkeypatch)
    live = ObjectId()
    _installFile(monkeypatch, {str(live): {"_id": live, "name": "a.nii.gz"}})
    job = _job(
        {"outA": str(live), "outB": str(ObjectId())}, [_spec("outA"), _spec("outB")]
    )

    results, missing = results_mod._collectJobResults(job, user=None)

    assert missing == 1 and len(results) == 1 and results[0]["id"] == str(live)


def test_collect_skips_identifier_without_a_declared_spec(monkeypatch):
    # slicer's returnparameterfile has no declared image/file spec: not projected,
    # and not counted as missing.
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(monkeypatch, {str(fid): {"_id": fid, "name": "params.txt"}})

    results, missing = results_mod._collectJobResults(
        _job({"returnparameterfile": str(fid)}, []), user=None
    )

    assert results == [] and missing == 0


def test_collect_declared_but_unrecorded_output_counts_missing(monkeypatch):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})
    results, missing = results_mod._collectJobResults(
        _job({}, [_spec("outVol")]), user=None
    )
    assert results == [] and missing == 1


def test_collect_labelmap_projects_add_segment_group_with_source(monkeypatch):
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(monkeypatch, {str(fid): {"_id": fid, "name": "seg.nii.gz"}})
    job = _job({"outSeg": str(fid)}, [_spec("outSeg", tag="image", isLabel=True)])

    results, _ = results_mod._collectJobResults(job, user=None)

    assert results[0]["intent"] == "add-segment-group"
    assert results[0]["source"] == {
        "providerId": "girder-slicer-cli:folder-1",
        "jobId": str(job["_id"]),
        "outputId": "outSeg",
    }


def test_collect_labelmap_carries_no_segments_payload(monkeypatch):
    # A `.seg.nrrd` labelmap embeds its segment names/colors, which the
    # client reads on load. The backend folds no JSON sidecar, so the
    # add-segment-group intent carries no `segments` payload.
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(monkeypatch, {str(fid): {"_id": fid, "name": "seg.seg.nrrd"}})
    job = _job({"outSeg": str(fid)}, [_spec("outSeg", tag="image", isLabel=True)])

    results, _ = results_mod._collectJobResults(job, user=None)

    assert len(results) == 1
    assert results[0]["intent"] == "add-segment-group"
    assert "segments" not in results[0]


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.INACTIVE,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.ERROR,
        JobStatus.CANCELED,
    ],
)
def test_payload_returns_typed_conflict_on_non_succeeded_job(monkeypatch, status):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})
    payload = results_mod._jobResultsPayload(_job({}, [], status=status), user=None)
    assert payload["code"] in {"results_not_ready", "results_unavailable"}
    assert payload["resultState"] in {"waiting", "unavailable"}


def test_payload_total_loss_is_incomplete_envelope(monkeypatch):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})  # every recorded output deleted
    job = _job({"outVol": str(ObjectId())}, [_spec("outVol")])
    payload = results_mod._jobResultsPayload(job, user=None)
    assert payload == {"resultState": "incomplete", "intents": [], "missing": 1}


def test_payload_returns_envelope_on_success(monkeypatch):
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(monkeypatch, {str(fid): {"_id": fid, "name": "out.nii.gz"}})
    payload = results_mod._jobResultsPayload(
        _job({"outVol": str(fid)}, [_spec("outVol")]), user=None
    )
    # The {intents, missing} envelope (contract jobResultsSchema); a clean success
    # reports missing == 0.
    assert isinstance(payload, dict)
    assert payload["resultState"] == "ready"
    assert len(payload["intents"]) == 1
    assert payload["missing"] == 0


def test_payload_empty_success_is_empty_envelope_not_an_error(monkeypatch):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})
    # Succeeded with nothing bound -> legit empty envelope (distinguishable from
    # deleted, which errors).
    assert results_mod._jobResultsPayload(_job({}, []), user=None) == {
        "resultState": "ready",
        "intents": [],
        "missing": 0,
    }


def test_payload_partial_loss_returns_resolved_and_missing_count(monkeypatch):
    _deterministicUrls(monkeypatch)
    live = ObjectId()
    _installFile(monkeypatch, {str(live): {"_id": live, "name": "a.nii.gz"}})
    job = _job(
        {"outA": str(live), "outB": str(ObjectId())}, [_spec("outA"), _spec("outB")]
    )
    payload = results_mod._jobResultsPayload(job, user=None)
    assert len(payload["intents"]) == 1
    assert payload["missing"] == 1
    assert payload["resultState"] == "incomplete"
