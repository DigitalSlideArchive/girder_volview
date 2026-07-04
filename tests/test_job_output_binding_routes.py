"""Server-fixture coverage for Chunk 17 reference-bound job outputs (D5).

What the offline ``test_job_output_binding`` unit tests cannot show, exercised
here against real Girder models + the live cherrypy pipeline:

1. *Real Mongo binding* -- ``_recordJobOutput`` records a file id under the output
   identifier via a dotted ``otherFields`` key, and real Mongo interprets it as a
   nested ``$set`` path (so ``_collectJobResults`` reads it back), N outputs each
   under their own key.
2. *End-to-end results route* -- a succeeded job's ``GET .../results`` returns the
   reference-bound intents with real ``makeFileDownloadUrl`` urls and real per-user
   File ACL, never a folder-name scan.
3. *Honest semantics* -- a non-succeeded job and a job whose output was deleted
   both return an explicit error (400), never a silent ``[]``.
4. *The race is gone* -- two jobs writing the SAME output filename into ONE folder
   resolve to their OWN bound file, proven against real Mongo.

Like ``test_staging_routes`` this needs a live pytest-girder server + Mongo; the
module self-skips when the test Mongo is unreachable so the offline gate stays
green, and runs (and must pass) wherever Mongo is present.
"""

import io
import json
import os
import socket
import types

import pytest

from girder_volview.facade import processing


# ---------------------------------------------------------------------------
# Self-skip when no live test Mongo is reachable (mirrors test_staging_routes)
# ---------------------------------------------------------------------------

def _mongo_reachable(timeout=0.5):
    host, port = "localhost", 27017
    uri = os.environ.get("GIRDER_TEST_DB", "")
    if uri.startswith("mongodb://"):
        netloc = uri[len("mongodb://"):].split("/", 1)[0].split(",", 1)[0]
        if ":" in netloc:
            host, port_str = netloc.rsplit(":", 1)
            port = int(port_str) if port_str.isdigit() else port
        elif netloc:
            host = netloc
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_staging_routes); unavailable offline",
)


_CLI_XML_IMAGE = (
    '<?xml version="1.0"?>'
    "<executable><category>Radiology</category><title>Otsu</title><parameters>"
    "<image><name>outVol</name><channel>output</channel></image>"
    "</parameters></executable>"
)

RESULTS_PATH = "/folder/%s/volview_processing/jobs/%s/results"


# ---------------------------------------------------------------------------
# Real users / folders
# ---------------------------------------------------------------------------

@pytest.fixture
def owner(db):
    from girder.models.user import User
    return User().createUser(
        login="resultowner", password="password123", firstName="A", lastName="B",
        email="resultowner@example.com", admin=False,
    )


@pytest.fixture
def ownerFolder(fsAssetstore, owner):
    from girder.models.folder import Folder
    return Folder().createFolder(
        owner, "launch", parentType="user", creator=owner, public=False
    )


# ---------------------------------------------------------------------------
# Helpers: bind a real job, record an output the way girder_worker's upload
# would (a reference-carrying data.process event under the job's own token)
# ---------------------------------------------------------------------------

def _reload(job):
    from girder_jobs.models.job import Job
    return Job().load(job["_id"], force=True)


def _makeBoundJob(owner, cli_xml=_CLI_XML_IMAGE):
    from girder.models.token import Token
    from girder_jobs.models.job import Job
    job = Job().createJob(title="t", type="volview_test", user=owner, public=False)
    token = Token().createToken(user=owner)
    processing._bindJobOutputs(job, token, cli_xml)
    return _reload(job), token


def _recordOutput(owner, folder, token, identifier, name, content=b"result-bytes"):
    from girder.models.upload import Upload
    fileDoc = Upload().uploadFromFile(
        io.BytesIO(content), size=len(content), name=name,
        parentType="folder", parent=folder, user=owner,
    )
    # Craft the event exactly as girder_worker's output upload delivers it: the
    # slicer_cli_web reference JSON (carrying the output identifier) + the uploaded
    # file, under THIS job's token (the correlation key). A server-side
    # uploadFromFile cannot reproduce the worker uploading under the job token, so
    # the handler is fired directly with that faithful event.
    event = types.SimpleNamespace(info={
        "reference": json.dumps(
            {"slicer_cli_web": {"name": "Otsu"}, "identifier": identifier, "uuid": "u"}
        ),
        "file": fileDoc,
        "currentToken": token,
    })
    processing._recordJobOutput(event)
    return fileDoc


def _succeed(job):
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job
    for status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCESS):
        job = Job().updateJob(_reload(job), status=status)
    return job


def _getResults(server, folder, user, jobId):
    return server.request(
        path=RESULTS_PATH % (folder["_id"], jobId),
        method="GET", user=user, isJson=True, exception=True,
    )


# ---------------------------------------------------------------------------
# 1. Real Mongo: the dotted otherFields key nests, and N outputs each bind
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_record_nests_file_id_under_identifier_in_mongo(server, owner, ownerFolder):
    job, token = _makeBoundJob(owner)
    fileDoc = _recordOutput(owner, ownerFolder, token, "outVol", "brain.otsu.nii.gz")

    reloaded = _reload(job)
    # Real Mongo interpreted the dotted $set key as a nested path.
    assert reloaded[processing._OUTPUTS_FIELD] == {"outVol": str(fileDoc["_id"])}


@pytest.mark.plugin("volview")
def test_n_outputs_each_bind_under_their_own_key(server, owner, ownerFolder):
    cli_xml = (
        '<?xml version="1.0"?>'
        "<executable><category>Radiology</category><title>Multi</title><parameters>"
        "<image><name>outA</name><channel>output</channel></image>"
        "<image><name>outB</name><channel>output</channel></image>"
        '<image type="label"><name>outC</name><channel>output</channel></image>'
        "</parameters></executable>"
    )
    job, token = _makeBoundJob(owner, cli_xml)
    fa = _recordOutput(owner, ownerFolder, token, "outA", "a.nii.gz")
    fb = _recordOutput(owner, ownerFolder, token, "outB", "b.nii.gz")
    fc = _recordOutput(owner, ownerFolder, token, "outC", "c.seg.nrrd")

    bound = _reload(job)[processing._OUTPUTS_FIELD]
    assert bound == {
        "outA": str(fa["_id"]), "outB": str(fb["_id"]), "outC": str(fc["_id"]),
    }

    resp = _getResults(server, ownerFolder, owner, _succeed(job)["_id"])
    assert resp.output_status.startswith(b"200")
    assert len(resp.json) == 3


# ---------------------------------------------------------------------------
# 2. End-to-end results route — reference-bound intent, real url + ACL
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_results_route_returns_reference_bound_intent(server, owner, ownerFolder):
    job, token = _makeBoundJob(owner)
    fileDoc = _recordOutput(owner, ownerFolder, token, "outVol", "brain.otsu.nii.gz")

    resp = _getResults(server, ownerFolder, owner, _succeed(job)["_id"])

    assert resp.output_status.startswith(b"200")
    assert len(resp.json) == 1
    assert resp.json[0]["id"] == str(fileDoc["_id"])
    assert resp.json[0]["intent"] == "add-base-image"
    # Built via makeFileDownloadUrl (origin-relative, filename-encoded), NOT the
    # retired hand-built f-string.
    assert resp.json[0]["url"] == "/api/v1/file/%s/proxiable/brain.otsu.nii.gz" % fileDoc["_id"]


# ---------------------------------------------------------------------------
# 3. Honest semantics — non-succeeded and deleted-output both 400, not []
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_results_route_errors_on_non_succeeded_job(server, owner, ownerFolder):
    job, token = _makeBoundJob(owner)
    _recordOutput(owner, ownerFolder, token, "outVol", "out.nii.gz")
    # Job left INACTIVE (never transitioned to a terminal success).

    resp = _getResults(server, ownerFolder, owner, job["_id"])
    assert resp.output_status.startswith(b"400")


@pytest.mark.plugin("volview")
def test_results_route_errors_when_bound_output_deleted(server, owner, ownerFolder):
    from girder.models.file import File
    job, token = _makeBoundJob(owner)
    fileDoc = _recordOutput(owner, ownerFolder, token, "outVol", "out.nii.gz")
    job = _succeed(job)

    File().remove(File().load(fileDoc["_id"], force=True))

    resp = _getResults(server, ownerFolder, owner, job["_id"])
    # Succeeded-but-output-deleted is an explicit error, never a silent empty list.
    assert resp.output_status.startswith(b"400")


# ---------------------------------------------------------------------------
# 4. The race is gone — two same-name jobs, one folder, own results
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_two_same_name_jobs_do_not_cross_results(server, owner, ownerFolder):
    jobA, tokenA = _makeBoundJob(owner)
    jobB, tokenB = _makeBoundJob(owner)

    # Both jobs write the SAME output filename into the SAME folder.
    fileA = _recordOutput(owner, ownerFolder, tokenA, "outVol", "out.nii.gz", content=b"AAAA")
    fileB = _recordOutput(owner, ownerFolder, tokenB, "outVol", "out.nii.gz", content=b"BBBB")
    assert fileA["_id"] != fileB["_id"]

    respA = _getResults(server, ownerFolder, owner, _succeed(jobA)["_id"])
    respB = _getResults(server, ownerFolder, owner, _succeed(jobB)["_id"])

    # Each job resolves ONLY the file bound to itself, though the names collide.
    assert respA.json[0]["id"] == str(fileA["_id"])
    assert respB.json[0]["id"] == str(fileB["_id"])
