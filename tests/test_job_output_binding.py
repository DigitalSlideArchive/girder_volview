"""Offline unit coverage for Chunk 17 reference-bound job outputs (D5).

Outputs bind to the job by REFERENCE, never by filename: a ``data.process``
handler records each uploaded output's file id ONTO the job keyed by output
identifier, and result collection reads those ids OFF the job (no folder-name
scan). ``getJobResults`` fails loud on a non-succeeded job and reports a
``missing`` count rather than a silent ``[]``.

These drive the pure control flow with fake Girder/Job/File models -- no live
Girder, same spirit as ``test_transient_cleanup`` -- so they run in the offline
gate too. The real Mongo-backed lifecycle (dotted ``$set`` nesting, real File
ACL, the results route, two same-name jobs against one folder) lives in
``test_job_output_binding_routes``.
"""

import json

import pytest
from bson.objectid import ObjectId

from girder.exceptions import RestException
from girder_jobs.constants import JobStatus

from girder_volview.facade import processing

_OUTPUTS = processing._OUTPUTS_FIELD
_SPECS = processing._OUTPUT_SPECS_FIELD
_TOKEN = processing._JOB_TOKEN_FIELD


# ---------------------------------------------------------------------------
# Fakes (no live Girder)
# ---------------------------------------------------------------------------

class _Event:
    def __init__(self, info):
        self.info = info


class _FakeJob:
    """Job() stand-in: load/findOne by token; ``updateJob`` records the otherFields
    it was called with AND applies dotted keys into a nested map so a follow-up read
    sees them (mirrors how Mongo interprets a dotted ``$set`` path)."""

    def __init__(self, jobs=None):
        self._byId = {str(j["_id"]): j for j in (jobs or [])}
        self.updated = []

    def load(self, jobId, force=False, exc=False, user=None, level=None):
        return self._byId.get(str(jobId))

    def findOne(self, query):
        token = query.get(_TOKEN)
        for job in self._byId.values():
            if job.get(_TOKEN) == token:
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


class _FakeFile:
    """File() stand-in: ``load`` returns a canned doc or None (a deleted file)."""

    def __init__(self, byId=None):
        self._byId = byId or {}

    def load(self, fileId, user=None, level=None, exc=False):
        return self._byId.get(str(fileId))


def _installFile(monkeypatch, byId=None):
    monkeypatch.setattr(processing, "File", lambda: _FakeFile(byId))


def _deterministicUrls(monkeypatch):
    # Avoid the getApiRoot/server dependency; the real url shape is covered in the
    # server-fixture suite.
    monkeypatch.setattr(
        processing,
        "makeFileDownloadUrl",
        lambda f: "/api/v1/file/%s/proxiable/%s" % (f["_id"], f["name"]),
    )


def _spec(name, tag="image", isLabel=False, ext=""):
    return {"name": name, "tag": tag, "isLabel": isLabel, "fileExtensions": ext}


def _job(outputs, specs, status=JobStatus.SUCCESS):
    return {"_id": ObjectId(), "status": status, _OUTPUTS: outputs, _SPECS: specs}


def _uploadEvent(reference, fileId, tokenId="tok-1"):
    return _Event({
        "reference": reference,
        "file": {"_id": fileId} if fileId is not None else None,
        "currentToken": {"_id": tokenId},
    })


# ---------------------------------------------------------------------------
# _parseOutputReference — fail closed on anything that is not a well-formed,
# safe output reference
# ---------------------------------------------------------------------------

def test_parse_reference_accepts_slicer_shaped_ref():
    ref = processing._parseOutputReference(
        json.dumps({"slicer_cli_web": {}, "identifier": "outVol", "uuid": "u"})
    )
    assert ref["identifier"] == "outVol"


def test_parse_reference_accepts_dict_passthrough():
    assert processing._parseOutputReference({"identifier": "x"})["identifier"] == "x"


def test_parse_reference_rejects_non_json_or_non_dict():
    assert processing._parseOutputReference(None) is None
    assert processing._parseOutputReference("not json") is None
    assert processing._parseOutputReference(json.dumps(["a", "b"])) is None


def test_parse_reference_requires_a_nonempty_identifier():
    assert processing._parseOutputReference(json.dumps({"uuid": "u"})) is None
    assert processing._parseOutputReference(json.dumps({"identifier": ""})) is None


def test_parse_reference_rejects_operator_or_dotted_identifier():
    # A dotted / $-bearing identifier could otherwise forge a nested or operator
    # $set key when recorded onto the job.
    assert processing._parseOutputReference(json.dumps({"identifier": "a.b"})) is None
    assert processing._parseOutputReference(json.dumps({"identifier": "$set"})) is None


# ---------------------------------------------------------------------------
# _jobForOutputUpload — reference→job correlation (by the job's own token)
# ---------------------------------------------------------------------------

def test_job_for_upload_correlates_by_token(monkeypatch):
    job = {"_id": ObjectId(), _TOKEN: "tok-1"}
    _installJob(monkeypatch, _FakeJob([job]))
    found = processing._jobForOutputUpload(
        {"identifier": "o"}, {"currentToken": {"_id": "tok-1"}}
    )
    assert found is job


def test_job_for_upload_honors_explicit_job_id(monkeypatch):
    job = {"_id": ObjectId()}
    _installJob(monkeypatch, _FakeJob([job]))
    found = processing._jobForOutputUpload(
        {"identifier": "o", "jobId": str(job["_id"])}, {}
    )
    assert found is job


def test_job_for_upload_uncorrelated_is_none(monkeypatch):
    _installJob(monkeypatch, _FakeJob([{"_id": ObjectId(), _TOKEN: "tok-1"}]))
    assert processing._jobForOutputUpload(
        {"identifier": "o"}, {"currentToken": {"_id": "stranger"}}
    ) is None
    assert processing._jobForOutputUpload({"identifier": "o"}, {}) is None


def test_job_for_upload_swallows_a_malformed_job_id(monkeypatch):
    class _RaisingJob:
        def load(self, jobId, force=False, exc=False):
            raise ValueError("bad id")

        def findOne(self, query):
            return None

    _installJob(monkeypatch, _RaisingJob())
    # A malformed jobId in the reference must fail closed, never escape the handler.
    assert processing._jobForOutputUpload(
        {"identifier": "o", "jobId": "not-an-id"}, {}
    ) is None


# ---------------------------------------------------------------------------
# _recordJobOutput — the data.process handler records ids onto the job
# ---------------------------------------------------------------------------

def test_record_output_binds_file_under_identifier(monkeypatch):
    job = {"_id": ObjectId(), _TOKEN: "tok-1", _OUTPUTS: {}}
    model = _installJob(monkeypatch, _FakeJob([job]))
    fid = ObjectId()

    processing._recordJobOutput(_uploadEvent(json.dumps({"identifier": "outVol"}), fid))

    # Recorded via a dotted key so Mongo $set nests per identifier.
    assert model.updated == [{"%s.outVol" % _OUTPUTS: str(fid)}]
    assert job[_OUTPUTS]["outVol"] == str(fid)


def test_record_output_n_outputs_all_bind_without_overwrite(monkeypatch):
    job = {"_id": ObjectId(), _TOKEN: "tok-1", _OUTPUTS: {}}
    _installJob(monkeypatch, _FakeJob([job]))

    expected = {}
    for name in ("outA", "outB", "outC"):
        fid = ObjectId()
        expected[name] = str(fid)
        processing._recordJobOutput(_uploadEvent(json.dumps({"identifier": name}), fid))

    # Each of the N outputs bound under its own key; none overwrote another.
    assert job[_OUTPUTS] == expected


def test_record_output_ignores_referenceless_or_bad_upload(monkeypatch):
    job = {"_id": ObjectId(), _TOKEN: "tok-1", _OUTPUTS: {}}
    model = _installJob(monkeypatch, _FakeJob([job]))

    processing._recordJobOutput(_uploadEvent(None, ObjectId()))       # no reference
    processing._recordJobOutput(_uploadEvent("not json", ObjectId()))  # bad reference
    processing._recordJobOutput(_uploadEvent(json.dumps({"identifier": "o"}), None))  # no file

    assert model.updated == []
    assert job[_OUTPUTS] == {}


def test_record_output_ignores_upload_that_matches_no_job(monkeypatch):
    # An upload whose token matches no facade job is never recorded onto some other
    # job -- the record-layer half of "two same-name jobs cannot cross".
    job = {"_id": ObjectId(), _TOKEN: "tok-1", _OUTPUTS: {}}
    model = _installJob(monkeypatch, _FakeJob([job]))

    processing._recordJobOutput(
        _uploadEvent(json.dumps({"identifier": "o"}), ObjectId(), tokenId="stranger")
    )

    assert model.updated == []
    assert job[_OUTPUTS] == {}


def test_record_output_ignores_malformed_event(monkeypatch):
    model = _installJob(monkeypatch, _FakeJob([]))
    processing._recordJobOutput(_Event(None))
    processing._recordJobOutput(_Event("not-a-dict"))
    assert model.updated == []


# ---------------------------------------------------------------------------
# _bindJobOutputs — submit-side stamping of specs + token + empty id map
# ---------------------------------------------------------------------------

def test_bind_job_outputs_records_specs_token_and_empty_map(monkeypatch):
    job = {"_id": ObjectId()}
    model = _installJob(monkeypatch, _FakeJob([job]))
    xml = (
        "<executable><parameters>"
        '<image type="label"><name>outSeg</name><channel>output</channel></image>'
        "</parameters></executable>"
    )

    processing._bindJobOutputs(job, {"_id": "tok-xyz"}, xml)

    assert len(model.updated) == 1
    fields = model.updated[0]
    assert fields[_TOKEN] == "tok-xyz"
    assert fields[_OUTPUTS] == {}
    assert fields[_SPECS] == [
        {"name": "outSeg", "tag": "image", "isLabel": True, "fileExtensions": ""}
    ]


# ---------------------------------------------------------------------------
# _collectJobResults — reads ids OFF the job, counts missing, never name-matches
# ---------------------------------------------------------------------------

def test_collect_reads_ids_off_the_job(monkeypatch):
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(monkeypatch, {
        str(fid): {"_id": fid, "name": "brain.otsu.nii.gz",
                   "mimeType": "application/octet-stream", "size": 10},
    })
    job = _job({"outVol": str(fid)}, [_spec("outVol")])

    results, missing = processing._collectJobResults(job, user=None)

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

    results, missing = processing._collectJobResults(_job(outputs, specs), user=None)

    assert missing == 0 and len(results) == 3


def test_collect_deleted_output_counts_missing(monkeypatch):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})  # load -> None (deleted)

    results, missing = processing._collectJobResults(
        _job({"outVol": str(ObjectId())}, [_spec("outVol")]), user=None
    )

    assert results == [] and missing == 1


def test_collect_partial_loss_returns_resolved_and_counts_missing(monkeypatch):
    _deterministicUrls(monkeypatch)
    live = ObjectId()
    _installFile(monkeypatch, {str(live): {"_id": live, "name": "a.nii.gz"}})
    job = _job({"outA": str(live), "outB": str(ObjectId())},
               [_spec("outA"), _spec("outB")])

    results, missing = processing._collectJobResults(job, user=None)

    assert missing == 1 and len(results) == 1 and results[0]["id"] == str(live)


def test_collect_two_same_name_jobs_do_not_cross(monkeypatch):
    _deterministicUrls(monkeypatch)
    fa, fb = ObjectId(), ObjectId()
    _installFile(monkeypatch, {
        str(fa): {"_id": fa, "name": "out.nii.gz"},
        str(fb): {"_id": fb, "name": "out.nii.gz"},  # SAME name, different file
    })
    specs = [_spec("outVol")]

    resultsA, _ = processing._collectJobResults(_job({"outVol": str(fa)}, specs), user=None)
    resultsB, _ = processing._collectJobResults(_job({"outVol": str(fb)}, specs), user=None)

    # Each job resolves ONLY its own recorded id, though the filenames collide.
    assert resultsA[0]["id"] == str(fa)
    assert resultsB[0]["id"] == str(fb)


def test_collect_skips_identifier_without_a_declared_spec(monkeypatch):
    # slicer's returnparameterfile has no declared image/file spec: not projected,
    # and not counted as missing.
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(monkeypatch, {str(fid): {"_id": fid, "name": "params.txt"}})

    results, missing = processing._collectJobResults(
        _job({"returnparameterfile": str(fid)}, []), user=None
    )

    assert results == [] and missing == 0


def test_collect_no_recorded_outputs_is_empty_not_missing(monkeypatch):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})
    results, missing = processing._collectJobResults(_job({}, [_spec("outVol")]), user=None)
    assert results == [] and missing == 0


def test_collect_labelmap_projects_add_segment_group_with_source(monkeypatch):
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(monkeypatch, {str(fid): {"_id": fid, "name": "seg.nii.gz"}})
    job = _job({"outSeg": str(fid)}, [_spec("outSeg", tag="image", isLabel=True)])

    results, _ = processing._collectJobResults(job, user=None)

    assert results[0]["intent"] == "add-segment-group"
    assert results[0]["source"] == {"jobId": str(job["_id"]), "outputId": "outSeg"}


def test_collect_folds_labels_sidecar_into_segments(monkeypatch):
    _deterministicUrls(monkeypatch)
    labels = [{"value": 1, "name": "liver", "color": [255, 0, 0, 255]}]
    monkeypatch.setattr(processing, "_readLabelsSidecar", lambda f: labels)
    segf, jsonf = ObjectId(), ObjectId()
    _installFile(monkeypatch, {
        str(segf): {"_id": segf, "name": "seg.nii.gz"},
        str(jsonf): {"_id": jsonf, "name": "seg.labels.json"},
    })
    job = _job(
        {"outSeg": str(segf), "outLabels": str(jsonf)},
        [_spec("outSeg", tag="image", isLabel=True), _spec("outLabels", tag="file")],
    )

    results, _ = processing._collectJobResults(job, user=None)

    # The .json sidecar folds into the labelmap's segments, not its own result.
    assert len(results) == 1
    assert results[0]["intent"] == "add-segment-group"
    assert results[0]["segments"] == labels


# ---------------------------------------------------------------------------
# _jobResultsPayload — honest semantics (non-succeeded/total-loss -> error;
# succeeded -> bare list; the client half shipped in Chunk 12 consumes this)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    JobStatus.INACTIVE, JobStatus.QUEUED, JobStatus.RUNNING,
    JobStatus.ERROR, JobStatus.CANCELED,
])
def test_payload_errors_on_non_succeeded_job(monkeypatch, status):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})
    with pytest.raises(RestException) as exc:
        processing._jobResultsPayload(_job({}, [], status=status), user=None)
    assert exc.value.code == 400


def test_payload_errors_when_all_outputs_unresolvable(monkeypatch):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})  # every recorded output deleted
    job = _job({"outVol": str(ObjectId())}, [_spec("outVol")])
    with pytest.raises(RestException) as exc:
        processing._jobResultsPayload(job, user=None)
    assert exc.value.code == 400
    assert "resolved" in str(exc.value)


def test_payload_returns_bare_list_on_success(monkeypatch):
    _deterministicUrls(monkeypatch)
    fid = ObjectId()
    _installFile(monkeypatch, {str(fid): {"_id": fid, "name": "out.nii.gz"}})
    payload = processing._jobResultsPayload(_job({"outVol": str(fid)}, [_spec("outVol")]), user=None)
    # A bare array, client-transparent (the client's zod parse rejects an object).
    assert isinstance(payload, list) and len(payload) == 1


def test_payload_empty_success_is_empty_list_not_an_error(monkeypatch):
    _deterministicUrls(monkeypatch)
    _installFile(monkeypatch, {})
    # Succeeded with nothing bound -> legit empty (distinguishable from deleted,
    # which errors).
    assert processing._jobResultsPayload(_job({}, []), user=None) == []


def test_payload_partial_loss_returns_resolved_not_an_error(monkeypatch):
    _deterministicUrls(monkeypatch)
    live = ObjectId()
    _installFile(monkeypatch, {str(live): {"_id": live, "name": "a.nii.gz"}})
    job = _job({"outA": str(live), "outB": str(ObjectId())},
               [_spec("outA"), _spec("outB")])
    payload = processing._jobResultsPayload(job, user=None)
    assert isinstance(payload, list) and len(payload) == 1
