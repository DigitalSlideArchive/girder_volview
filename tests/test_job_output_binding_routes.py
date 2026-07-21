"""Live-server coverage for folder-owned job outputs.

Exercises against real Girder models + the cherrypy pipeline what the offline
``test_job_output_binding`` unit tests cannot: real Mongo dotted-key binding
correlated by the finalized file's actual parent folder, the end-to-end results
route with real download urls and per-user File ACL, honest envelopes for
non-succeeded jobs and deleted outputs, per-job output-folder isolation, the
folder's submitter-only privacy, and first-insert folder ownership.

Needs a live pytest-girder server + Mongo; self-skips when the test Mongo is
unreachable.
"""

import io
import json
from conftest import _reload, mongo_reachable
import types
import uuid

import jsonschema
import pytest
from bson.objectid import ObjectId

from girder_volview.backend import inputs, outputs, routes, slicer_spec, submit
from girder_volview.utils import JOB_OUTPUT_FOLDER_META_KEY


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo; unavailable offline",
)


_CLI_XML_IMAGE = (
    '<?xml version="1.0"?>'
    "<executable><category>Radiology</category><title>Otsu</title><parameters>"
    "<image><name>outVol</name><channel>output</channel></image>"
    "</parameters></executable>"
)

# Job-addressed: results are keyed by job id alone, no folder.
RESULTS_PATH = "/volview_processing/jobs/%s/results"
FOLDER_MANIFEST_PATH = "/folder/%s/volview"
RUN_PATH = "/folder/%s/volview_processing/tasks/sometask/run"


def _makeBoundJob(owner, launchFolder, cli_xml=_CLI_XML_IMAGE):
    """Create the job's REAL private output folder (server-owned, submitter-only
    ADMIN, marked) and a job that owns it by ``_OUTPUT_FOLDER_ID_FIELD``."""
    from girder_jobs.models.job import Job

    outputFolder = routes._createJobOutputFolder(launchFolder, owner, uuid.uuid4().hex)
    job = Job().createJob(
        title="t",
        type="volview_test",
        user=owner,
        public=False,
        otherFields={
            inputs._LAUNCH_FOLDER_FIELD: str(launchFolder["_id"]),
            outputs._OUTPUT_SPECS_FIELD: slicer_spec.parse_cli(cli_xml)["outputs"],
            outputs._OUTPUT_FOLDER_ID_FIELD: str(outputFolder["_id"]),
            outputs._OUTPUTS_FIELD: {},
        },
    )
    return _reload(job), outputFolder


def _recordOutput(owner, outputFolder, identifier, name, content=b"result-bytes"):
    """Upload an output file INTO the job's private output folder. The finalization
    handler correlates it by that folder alone -- no token, no jobId."""
    from girder.models.upload import Upload

    reference = json.dumps(
        {
            "slicer_cli_web": {"name": "Otsu"},
            "identifier": identifier,
            "uuid": "u",
        }
    )
    return Upload().uploadFromFile(
        io.BytesIO(content),
        size=len(content),
        name=name,
        parentType="folder",
        parent=outputFolder,
        user=owner,
        reference=reference,
    )


def _succeed(job):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    for status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCESS):
        job = Job().updateJob(_reload(job), status=status)
    return job


def _getResults(server, user, jobId):
    return server.request(
        path=RESULTS_PATH % jobId,
        method="GET",
        user=user,
        isJson=True,
        exception=True,
    )


def _assertValidJobResults(payload):
    """Validate a LIVE results payload against the generated job-results schema."""
    import contract_loader

    schema = contract_loader.load_generated_schema("job-results")
    jsonschema.Draft202012Validator(schema).validate(payload)


def _assertValidJobResultsError(payload):
    import contract_loader

    schema = contract_loader.load_generated_schema("job-results-error")
    jsonschema.Draft202012Validator(schema).validate(payload)


@pytest.mark.plugin("volview")
def test_zero_outputs_records_nothing(server, owner, ownerFolder):
    # A declared-but-unproduced output leaves the recorded map empty (fail closed);
    # nothing correlates without a finalized file in the job's folder.
    job, _outputFolder = _makeBoundJob(owner, ownerFolder)
    assert _reload(job)[outputs._OUTPUTS_FIELD] == {}

    resp = _getResults(server, owner, _succeed(job)["_id"])
    assert resp.output_status.startswith(b"200")
    assert resp.json == {"resultState": "incomplete", "intents": [], "missing": 1}


@pytest.fixture
def _make(owner, ownerFolder):
    """Bind ``_makeBoundJob`` to the launch folder."""

    def factory(cli_xml=_CLI_XML_IMAGE):
        return _makeBoundJob(owner, ownerFolder, cli_xml)

    return factory


@pytest.mark.plugin("volview")
def test_record_nests_file_id_under_identifier_in_mongo(server, owner, _make):
    job, outputFolder = _make()
    fileDoc = _recordOutput(owner, outputFolder, "outVol", "brain.otsu.nii.gz")

    reloaded = _reload(job)
    # Real Mongo interpreted the dotted $set key as a nested path, keyed by the
    # file's ACTUAL private parent folder.
    assert reloaded[outputs._OUTPUTS_FIELD] == {"outVol": str(fileDoc["_id"])}


@pytest.mark.plugin("volview")
def test_n_outputs_each_bind_under_their_own_key(server, owner, _make):
    cli_xml = (
        '<?xml version="1.0"?>'
        "<executable><category>Radiology</category><title>Multi</title><parameters>"
        "<image><name>outA</name><channel>output</channel></image>"
        "<image><name>outB</name><channel>output</channel></image>"
        '<image type="label"><name>outC</name><channel>output</channel></image>'
        "</parameters></executable>"
    )
    job, outputFolder = _make(cli_xml)
    fa = _recordOutput(owner, outputFolder, "outA", "a.nii.gz")
    fb = _recordOutput(owner, outputFolder, "outB", "b.nii.gz")
    fc = _recordOutput(owner, outputFolder, "outC", "c.seg.nrrd")

    bound = _reload(job)[outputs._OUTPUTS_FIELD]
    assert bound == {
        "outA": str(fa["_id"]),
        "outB": str(fb["_id"]),
        "outC": str(fc["_id"]),
    }

    resp = _getResults(server, owner, _succeed(job)["_id"])
    assert resp.output_status.startswith(b"200")
    _assertValidJobResults(resp.json)
    assert len(resp.json["intents"]) == 3
    assert resp.json["missing"] == 0


@pytest.mark.plugin("volview")
def test_results_route_returns_folder_bound_intent(server, owner, _make):
    job, outputFolder = _make()
    fileDoc = _recordOutput(owner, outputFolder, "outVol", "brain.otsu.nii.gz")

    resp = _getResults(server, owner, _succeed(job)["_id"])

    assert resp.output_status.startswith(b"200")
    _assertValidJobResults(resp.json)
    assert resp.json["missing"] == 0
    intents = resp.json["intents"]
    assert len(intents) == 1
    assert intents[0]["id"] == str(fileDoc["_id"])
    assert intents[0]["intent"] == "add-base-image"
    # Built via makeFileDownloadUrl: origin-relative and filename-encoded.
    assert (
        intents[0]["url"]
        == "/api/v1/file/%s/proxiable/brain.otsu.nii.gz" % fileDoc["_id"]
    )


@pytest.mark.plugin("volview")
def test_results_route_errors_on_non_succeeded_job(server, owner, _make):
    job, outputFolder = _make()
    _recordOutput(owner, outputFolder, "outVol", "out.nii.gz")
    # Job left INACTIVE (never transitioned to a terminal success).

    resp = _getResults(server, owner, job["_id"])
    assert resp.output_status.startswith(b"409")
    _assertValidJobResultsError(resp.json)
    assert resp.json["code"] == "results_not_ready"
    assert resp.json["resultState"] == "waiting"
    assert resp.headers["Retry-After"] == "2"


@pytest.mark.plugin("volview")
def test_results_route_reports_unrecorded_success_as_incomplete(server, owner, _make):
    job, _outputFolder = _make()
    job = _succeed(job)

    resp = _getResults(server, owner, job["_id"])

    assert resp.output_status.startswith(b"200")
    _assertValidJobResults(resp.json)
    assert resp.json == {
        "resultState": "incomplete",
        "intents": [],
        "missing": 1,
    }


@pytest.mark.plugin("volview")
def test_results_route_total_loss_returns_incomplete(server, owner, _make):
    from girder.models.file import File

    job, outputFolder = _make()
    fileDoc = _recordOutput(owner, outputFolder, "outVol", "out.nii.gz")
    job = _succeed(job)

    File().remove(File().load(fileDoc["_id"], force=True))

    resp = _getResults(server, owner, job["_id"])
    assert resp.output_status.startswith(b"200")
    _assertValidJobResults(resp.json)
    assert resp.json == {
        "resultState": "incomplete",
        "intents": [],
        "missing": 1,
    }


@pytest.mark.plugin("volview")
def test_results_route_partial_miss_returns_survivor_and_missing_count(
    server, owner, _make
):
    from girder.models.file import File

    cli_xml = (
        '<?xml version="1.0"?>'
        "<executable><category>Radiology</category><title>Two</title><parameters>"
        "<image><name>outA</name><channel>output</channel></image>"
        "<image><name>outB</name><channel>output</channel></image>"
        "</parameters></executable>"
    )
    job, outputFolder = _make(cli_xml)
    survivor = _recordOutput(owner, outputFolder, "outA", "a.nii.gz")
    doomed = _recordOutput(owner, outputFolder, "outB", "b.nii.gz")
    job = _succeed(job)

    # One of the two bound outputs is deleted before the read.
    File().remove(File().load(doomed["_id"], force=True))

    resp = _getResults(server, owner, job["_id"])
    # A PARTIAL loss is NOT an error (unlike total loss): the survivor rides
    # `intents`, the deleted output rides the `missing` count.
    assert resp.output_status.startswith(b"200")
    _assertValidJobResults(resp.json)
    assert len(resp.json["intents"]) == 1
    assert resp.json["intents"][0]["id"] == str(survivor["_id"])
    assert resp.json["missing"] == 1
    assert resp.json["resultState"] == "incomplete"


@pytest.mark.plugin("volview")
def test_two_jobs_output_folders_never_cross(server, owner, ownerFolder):
    # Each job owns its OWN private output folder; an upload correlates ONLY by the
    # folder it lands in, so two jobs' recordings never cross.
    jobA, folderA = _makeBoundJob(owner, ownerFolder)
    jobB, folderB = _makeBoundJob(owner, ownerFolder)
    assert folderA["_id"] != folderB["_id"]

    fileA = _recordOutput(owner, folderA, "outVol", "out.nii.gz", content=b"AAAA")
    fileB = _recordOutput(owner, folderB, "outVol", "out.nii.gz", content=b"BBBB")

    assert _reload(jobA)[outputs._OUTPUTS_FIELD] == {"outVol": str(fileA["_id"])}
    assert _reload(jobB)[outputs._OUTPUTS_FIELD] == {"outVol": str(fileB["_id"])}


@pytest.mark.plugin("volview")
def test_two_same_name_jobs_do_not_cross_results(server, owner, ownerFolder):
    # Two jobs write the SAME output filename, but each into its OWN private output
    # folder. Each results read resolves ONLY the file bound to itself.
    jobA, folderA = _makeBoundJob(owner, ownerFolder)
    jobB, folderB = _makeBoundJob(owner, ownerFolder)

    fileA = _recordOutput(owner, folderA, "outVol", "out.nii.gz", content=b"AAAA")
    fileB = _recordOutput(owner, folderB, "outVol", "out.nii.gz", content=b"BBBB")
    assert fileA["_id"] != fileB["_id"]

    respA = _getResults(server, owner, _succeed(jobA)["_id"])
    respB = _getResults(server, owner, _succeed(jobB)["_id"])

    assert respA.json["intents"][0]["id"] == str(fileA["_id"])
    assert respB.json["intents"][0]["id"] == str(fileB["_id"])


@pytest.mark.plugin("volview")
def test_stranger_cannot_read_or_write_the_private_output_folder(
    server, owner, stranger, ownerFolder
):
    _job, outputFolder = _makeBoundJob(owner, ownerFolder)

    # READ: the submitter-only ADMIN ACL (createFolder's copied launch-folder ACL
    # was REPLACED) blocks a stranger from even loading the folder.
    read = server.request(
        path="/folder/%s" % outputFolder["_id"],
        method="GET",
        user=stranger,
        isJson=False,
        exception=True,
    )
    assert read.output_status.startswith(b"403")

    # WRITE: a stranger cannot create an item inside the private output folder.
    write = server.request(
        path="/item",
        method="POST",
        user=stranger,
        params={"folderId": str(outputFolder["_id"]), "name": "intruder.nrrd"},
        isJson=False,
        exception=True,
    )
    assert write.output_status.startswith(b"403")


@pytest.mark.plugin("volview")
def test_output_folder_files_absent_from_ordinary_launch_listing(
    server, owner, ownerFolder
):
    from girder.models.file import File

    base = "brain.nrrd"
    from girder.models.upload import Upload

    Upload().uploadFromFile(
        io.BytesIO(b"pixels"),
        size=6,
        name=base,
        parentType="folder",
        parent=ownerFolder,
        user=owner,
    )
    _job, outputFolder = _makeBoundJob(owner, ownerFolder)
    output = _recordOutput(owner, outputFolder, "outVol", "brain.otsu.seg.nrrd")

    resp = server.request(
        path=FOLDER_MANIFEST_PATH % ownerFolder["_id"],
        method="GET",
        user=owner,
        isJson=True,
        exception=True,
    )
    assert resp.output_status.startswith(b"200")
    names = {s.get("name") for s in resp.json["resources"]}
    assert base in names
    # The output lives in the private (marked) output subfolder, so it is excluded
    # from the ordinary launch listing (results take the job path only)...
    assert "brain.otsu.seg.nrrd" not in names
    # ...yet it stays durable and readable (re-fetched via the job/results path).
    assert File().load(output["_id"], user=owner, level=0, exc=False) is not None


@pytest.fixture
def runStub(monkeypatch):
    """Stub slicer_cli_web so runTask reaches job creation without docker, and
    create a REAL Girder job from the prepared initial fields."""
    cli = types.SimpleNamespace(name="Otsu", xml=_CLI_XML_IMAGE)
    monkeypatch.setattr(submit, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(
        submit,
        "_findScopedCliItem",
        lambda taskId, user: (cli, slicer_spec.parse_cli(cli.xml)),
    )

    def fake_gen(cliItem, params, user, initialFields):
        from girder_jobs.models.job import Job

        return Job().createJob(
            title="stub",
            type="volview_test",
            user=user,
            public=False,
            otherFields=initialFields,
        )

    monkeypatch.setattr(routes, "_genDockerJob", fake_gen)


@pytest.mark.plugin("volview")
def test_runtask_created_job_already_owns_output_folder(
    server, owner, ownerFolder, runStub
):
    from girder.models.folder import Folder
    from girder_jobs.models.job import Job

    resp = server.request(
        path=RUN_PATH % ownerFolder["_id"],
        method="POST",
        user=owner,
        body=json.dumps({"values": {"outVol": {"name": "result.nii.gz"}}}),
        type="application/json",
        isJson=True,
        exception=True,
    )
    assert resp.output_status.startswith(b"200")

    # The output-folder id was part of the FIRST insert: reload the just-created job
    # and it already carries the ownership key (no worker upload has happened yet).
    job = Job().load(resp.json["jobId"], force=True)
    folderId = job[outputs._OUTPUT_FOLDER_ID_FIELD]
    assert folderId
    assert ObjectId(folderId)  # a real folder id, not a placeholder

    # ...and it is a real, private, marked folder nested in the launch folder's
    # volview-jobs container.
    outputFolder = Folder().load(folderId, force=True)
    container = Folder().load(outputFolder["parentId"], force=True)
    assert container["name"] == routes.JOBS_CONTAINER_NAME
    assert str(container["parentId"]) == str(ownerFolder["_id"])
    assert outputFolder["public"] is False
    assert outputFolder["meta"][JOB_OUTPUT_FOLDER_META_KEY] is True


@pytest.mark.plugin("volview")
def test_runtask_discards_client_output_name_traversal(
    server, owner, ownerFolder, runStub
):
    """A crafted client output name never reaches the CLI -- the server
    overwrites it with a safe deterministic basename."""
    from girder_jobs.models.job import Job

    resp = server.request(
        path=RUN_PATH % ownerFolder["_id"],
        method="POST",
        user=owner,
        body=json.dumps({"values": {"outVol": {"name": "../../../../etc/passwd"}}}),
        type="application/json",
        isJson=True,
        exception=True,
    )
    assert resp.output_status.startswith(b"200")

    job = Job().load(resp.json["jobId"], force=True)
    stored = job[routes._SUBMITTED_PARAMETERS_FIELD]["outVol"]["name"]
    assert ".." not in stored
    assert "/" not in stored
    assert stored == "output.Otsu.outVol.nii.gz"


@pytest.mark.plugin("volview")
def test_runtask_rejects_undeclared_param(server, owner, ownerFolder, runStub):
    """A submission key the CLI does not declare is a 400 at the boundary."""
    resp = server.request(
        path=RUN_PATH % ownerFolder["_id"],
        method="POST",
        user=owner,
        body=json.dumps({"values": {"bogus": 1}}),
        type="application/json",
        isJson=True,
        exception=False,
    )
    assert resp.output_status.startswith(b"400")
