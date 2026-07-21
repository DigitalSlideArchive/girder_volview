"""Server-fixture coverage for the staging endpoint + transient cleanup, against
real Girder models and the live cherrypy pipeline.

Needs a live pytest-girder server + Mongo; the module self-skips when the test
Mongo is unreachable so the offline gate stays green.
"""

import datetime
import json
from conftest import mongo_reachable
import types

import pytest

from girder_volview.backend import inputs, routes, slicer_spec, submit
from girder_volview.utils import makeFileDownloadUrl


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo; unavailable offline",
)


_CLI_XML = (
    '<?xml version="1.0"?>'
    "<executable><category>Segmentation</category><title>Seg</title>"
    "<parameters>"
    '<image type="label"><name>inputVolume</name><channel>input</channel></image>'
    "</parameters></executable>"
)

STAGE_PATH = "/folder/%s/volview_processing/stage"
RUN_PATH = "/folder/%s/volview_processing/tasks/sometask/run"


@pytest.fixture
def realJobStub(monkeypatch):
    """Stub the slicer_cli_web touch points so runTask reaches transient marking
    without docker, but create a REAL Girder job so the terminal transition fires
    the real jobs.job.update.after handler."""
    cli = types.SimpleNamespace(name="Seg", xml=_CLI_XML)
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
    return cli


def _durable_reference(folder, user):
    import io
    from girder.models.upload import Upload

    return Upload().uploadFromFile(
        io.BytesIO(b"pixels"),
        size=6,
        name="reference.nrrd",
        parentType="folder",
        parent=folder,
        user=user,
    )


def _multipart_stage_body(content, name, reference_uri):
    boundary = "volview-stage-boundary"
    descriptor = json.dumps(
        {
            "type": "labelmap",
            "name": name,
            "referenceImage": {"type": "image", "uris": [reference_uri]},
        }
    ).encode("utf8")
    body = b"".join(
        [
            ("--%s\r\n" % boundary).encode(),
            b'Content-Disposition: form-data; name="descriptor"\r\n\r\n',
            descriptor,
            b"\r\n",
            ("--%s\r\n" % boundary).encode(),
            (
                'Content-Disposition: form-data; name="file"; filename="%s"\r\n' % name
            ).encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            content,
            b"\r\n",
            ("--%s--\r\n" % boundary).encode(),
        ]
    )
    return body, "multipart/form-data; boundary=%s" % boundary


def _stage(
    server,
    folder,
    user,
    content,
    name="staged.bin",
    isJson=True,
    reference_uri=None,
):
    reference = None if reference_uri else _durable_reference(folder, user)
    body, content_type = _multipart_stage_body(
        content,
        name,
        reference_uri or makeFileDownloadUrl(reference),
    )
    return server.request(
        path=STAGE_PATH % folder["_id"],
        method="POST",
        user=user,
        body=body,
        type=content_type,
        isJson=isJson,
        exception=True,
    )


def _run(server, folder, user, values):
    return server.request(
        path=RUN_PATH % folder["_id"],
        method="POST",
        user=user,
        body=json.dumps({"values": values}),
        type="application/json",
        isJson=True,
        exception=True,
    )


def _itemForUri(uri):
    from girder.models.file import File
    from girder.models.item import Item

    fileId = inputs._fileIdFromMintedUri(uri)
    fileDoc = File().load(fileId, force=True)
    return Item().load(fileDoc["itemId"], force=True)


@pytest.mark.plugin("volview")
def test_stage_returns_minted_uri_that_resolves(server, owner, ownerFolder):
    from girder.models.folder import Folder

    resp = _stage(server, ownerFolder, owner, b"seg-bytes", name="seg.seg.nrrd")

    assert resp.output_status.startswith(b"200")
    uris = resp.json["uris"]
    assert isinstance(uris, list) and len(uris) == 1
    # The backend minted an origin-relative proxiable URI (never the client).
    assert uris[0].startswith("/api/v1/file/")

    # It resolves through the SAME own-scheme path as any minted input; the CLI
    # param is the recovered file id.
    fileId = inputs._fileIdFromMintedUri(uris[0])
    assert fileId is not None
    params, _ = submit._translateValuesToSlicerParams(
        {"inputVolume": {"type": "labelmap", "uris": uris}},
        user=owner,
        outputFolder=ownerFolder,
    )
    assert params["inputVolume"] == fileId

    # The staged item is tagged transient and kept in the server-owned jobs
    # container rather than appearing beside the user's source image.
    stagedItem = _itemForUri(uris[0])
    jobsFolder = Folder().findOne(
        {
            "parentId": ownerFolder["_id"],
            "parentCollection": "folder",
            "name": routes.JOBS_CONTAINER_NAME,
        }
    )
    assert jobsFolder is not None
    assert str(stagedItem["folderId"]) == str(jobsFolder["_id"])
    assert str(stagedItem["folderId"]) != str(ownerFolder["_id"])
    assert stagedItem["meta"]["volviewTransient"] is True


@pytest.mark.plugin("volview")
def test_stage_does_not_sniff_labelmap_bytes(server, owner, ownerFolder):
    # Arbitrary bytes stage identically -- the typed descriptor, not byte
    # sniffing or an extension allow-list, supplies the labelmap semantics.
    resp = _stage(
        server, ownerFolder, owner, b"\x00\x01not a labelmap", name="blob.dat"
    )
    assert resp.output_status.startswith(b"200")
    assert inputs._fileIdFromMintedUri(resp.json["uris"][0]) is not None


@pytest.mark.plugin("volview")
def test_stage_rejects_foreign_reference_uri(server, owner, ownerFolder):
    resp = _stage(
        server,
        ownerFolder,
        owner,
        b"mask",
        reference_uri="https://foreign/image",
        isJson=False,
    )
    assert resp.output_status.startswith(b"400")


@pytest.mark.plugin("volview")
def test_stage_rejects_a_transient_reference(server, owner, ownerFolder):
    transient_uri = _stage(
        server, ownerFolder, owner, b"first", name="first.seg.nrrd"
    ).json["uris"][0]
    resp = _stage(
        server,
        ownerFolder,
        owner,
        b"second",
        name="second.seg.nrrd",
        reference_uri=transient_uri,
        isJson=False,
    )
    assert resp.output_status.startswith(b"400")


@pytest.mark.plugin("volview")
def test_stage_tag_failure_leaves_no_untagged_item(
    server, owner, ownerFolder, monkeypatch
):
    # Staging publication must be atomic-on-error: if the transient tag cannot
    # be applied after the upload finalizes, the item is deleted and the
    # request fails. An untagged leftover would be invisible to the TTL sweep
    # and surface as ordinary launch data.
    from girder.models.folder import Folder

    def boom(fileDoc):
        raise Exception("injected tagging failure")

    monkeypatch.setattr(inputs, "_tagItemTransient", boom)

    resp = _stage(
        server, ownerFolder, owner, b"mask", name="orphan.seg.nrrd", isJson=False
    )

    assert resp.output_status.startswith(b"500")
    jobsFolder = Folder().findOne(
        {
            "parentId": ownerFolder["_id"],
            "parentCollection": "folder",
            "name": routes.JOBS_CONTAINER_NAME,
        }
    )
    assert jobsFolder is not None
    names = [item["name"] for item in Folder().childItems(jobsFolder)]
    assert "orphan.seg.nrrd" not in names


@pytest.mark.plugin("volview")
def test_staged_input_copied_per_job_and_copy_deleted_at_terminal(
    server, owner, ownerFolder, realJobStub
):
    from girder.models.item import Item
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    from girder_volview.backend.outputs import _OUTPUT_FOLDER_ID_FIELD

    uri = _stage(server, ownerFolder, owner, b"labelmap", name="seg.seg.nrrd").json[
        "uris"
    ][0]
    stagedItem = _itemForUri(uri)
    assert stagedItem["meta"]["volviewTransient"] is True

    resp = _run(
        server, ownerFolder, owner, {"inputVolume": {"type": "labelmap", "uris": [uri]}}
    )
    assert resp.output_status.startswith(b"200")

    job = Job().load(resp.json["jobId"], force=True)
    # The job owns a private COPY of the staged input: recorded for cleanup,
    # parented in the job's private folder, still transient-tagged. The staged
    # ORIGINAL is never a job dependency.
    copies = job.get("volviewTransient", [])
    assert len(copies) == 1
    assert copies[0] != str(stagedItem["_id"])
    copyItem = Item().load(copies[0], force=True)
    assert str(copyItem["folderId"]) == job[_OUTPUT_FOLDER_ID_FIELD]
    assert copyItem["meta"]["volviewTransient"] is True

    # Legal transition chain to a terminal state (INACTIVE->QUEUED->RUNNING->SUCCESS).
    Job().updateJob(job, status=JobStatus.QUEUED)
    Job().updateJob(job, status=JobStatus.RUNNING)
    Job().updateJob(job, status=JobStatus.SUCCESS)

    # The real jobs.job.update.after handler deleted the job's OWN copy at
    # terminal; the staged original survives for reuse (TTL sweep owns it).
    assert Item().load(copies[0], force=True) is None
    assert Item().load(stagedItem["_id"], force=True) is not None


@pytest.mark.plugin("volview")
def test_concurrent_jobs_reusing_one_staged_input_do_not_interfere(
    server, owner, ownerFolder, realJobStub
):
    # Two jobs bound to the SAME staged input: the first to reach terminal must
    # not delete anything the second depends on.
    from girder.models.item import Item
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    uri = _stage(server, ownerFolder, owner, b"labelmap", name="seg.seg.nrrd").json[
        "uris"
    ][0]
    value = {"inputVolume": {"type": "labelmap", "uris": [uri]}}

    jobA = Job().load(_run(server, ownerFolder, owner, value).json["jobId"], force=True)
    jobB = Job().load(_run(server, ownerFolder, owner, value).json["jobId"], force=True)
    copyA = jobA.get("volviewTransient", [])[0]
    copyB = jobB.get("volviewTransient", [])[0]
    assert copyA != copyB

    # Job A terminates (its whole lifecycle) while job B is still queued.
    Job().updateJob(jobA, status=JobStatus.QUEUED)
    Job().updateJob(jobA, status=JobStatus.RUNNING)
    Job().updateJob(jobA, status=JobStatus.SUCCESS)

    # A's copy is gone; B's copy is untouched and B can still run to terminal.
    assert Item().load(copyA, force=True) is None
    assert Item().load(copyB, force=True) is not None
    Job().updateJob(jobB, status=JobStatus.QUEUED)
    Job().updateJob(jobB, status=JobStatus.RUNNING)
    Job().updateJob(jobB, status=JobStatus.SUCCESS)
    assert Item().load(copyB, force=True) is None


@pytest.mark.plugin("volview")
def test_non_transient_input_survives_job_terminal(
    server, owner, ownerFolder, realJobStub
):
    # A regular (non-staged) input is never recorded and never deleted at job end.
    import io

    from girder.models.item import Item
    from girder.models.upload import Upload
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job

    from girder_volview.utils import makeFileDownloadUrl as mint

    durable = Upload().uploadFromFile(
        io.BytesIO(b"pixels"),
        size=6,
        name="scan.nrrd",
        parentType="folder",
        parent=ownerFolder,
        user=owner,
    )
    durableItemId = durable["itemId"]
    value = {"type": "image", "uris": [mint(durable)]}
    resp = _run(server, ownerFolder, owner, {"inputVolume": value})
    job = Job().load(resp.json["jobId"], force=True)
    assert not job.get("volviewTransient")

    Job().updateJob(job, status=JobStatus.QUEUED)
    Job().updateJob(job, status=JobStatus.RUNNING)
    Job().updateJob(job, status=JobStatus.SUCCESS)

    assert Item().load(durableItemId, force=True) is not None


@pytest.mark.plugin("volview")
def test_orphan_older_than_ttl_swept_on_next_stage(server, owner, ownerFolder):
    from girder.models.item import Item

    oldUri = _stage(server, ownerFolder, owner, b"old", name="old.bin").json["uris"][0]
    youngUri = _stage(server, ownerFolder, owner, b"young", name="young.bin").json[
        "uris"
    ][0]
    oldItem = _itemForUri(oldUri)
    youngItem = _itemForUri(youngUri)

    # Backdate the old item beyond the TTL (the marker carries no timestamp, so
    # the sweep keys off item['created']).
    stale = (
        datetime.datetime.utcnow()
        - inputs._TRANSIENT_ORPHAN_TTL
        - datetime.timedelta(hours=1)
    )
    Item().collection.update_one({"_id": oldItem["_id"]}, {"$set": {"created": stale}})

    # A third staging call sweeps at the top of the handler.
    newUri = _stage(server, ownerFolder, owner, b"new", name="new.bin").json["uris"][0]
    newItem = _itemForUri(newUri)

    assert Item().load(oldItem["_id"], force=True) is None  # swept
    assert Item().load(youngItem["_id"], force=True) is not None  # within TTL
    assert Item().load(newItem["_id"], force=True) is not None  # just staged
