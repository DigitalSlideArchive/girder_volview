import cherrypy
import errno

from girder import plugin

from girder.api.describe import Description, autoDescribeRoute
from girder.api import access
from girder.api.rest import boundHandler, setResponseHeader, setContentDisposition
from girder.constants import AccessType, TokenScope

# saveSession
from girder.models.file import File as FileModel
from girder.models.upload import Upload
from girder.models.item import Item
from girder.utility import RequestBodyStream
from girder.exceptions import GirderException

# downloadDatasets
from girder.models.item import Item as ItemModel
from girder.utility import ziputil


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description("Save VolView session in an item")
    .param("itemId", "The item ID", paramType="path")
    .errorResponse()
)
def saveSession(self, itemId):
    size = int(cherrypy.request.headers.get("Content-Length"))
    if size == 0:
        raise GirderException(
            "Expected non-zero Content-Length header", "girder.api.v1.item.save-volview"
        )

    # modified from girder.api.v1.file.File.initUpload
    fileModel = FileModel()
    parentId = itemId
    parentType = "item"
    name = "session.volview.zip"
    mimeType = "application/zip"
    reference = None
    user = self.getCurrentUser()
    parent = Item().load(id=parentId, user=user, level=AccessType.WRITE, exc=True)

    assetstore = None

    chunk = None
    ct = cherrypy.request.body.content_type.value
    if (
        ct not in cherrypy.request.body.processors
        and ct.split("/", 1)[0] not in cherrypy.request.body.processors
    ):
        chunk = RequestBodyStream(cherrypy.request.body)
    if chunk is not None and chunk.getSize() <= 0:
        chunk = None

    try:
        upload = Upload().createUpload(
            user=user,
            name=name,
            parentType=parentType,
            parent=parent,
            size=size,
            mimeType=mimeType,
            reference=reference,
            assetstore=assetstore,
        )
    except OSError as exc:
        if exc.errno == errno.EACCES:
            raise GirderException(
                "Failed to create upload.",
                "girder.api.v1.item.volview.create-upload-failed",
            )
        raise
    if upload["size"] > 0:
        if chunk:
            return Upload().handleChunk(upload, chunk, filter=True, user=user)

        return upload
    else:
        return fileModel.filter(Upload().finalizeUpload(upload), user)


def isSessionFile(path):
    if path.endswith("volview.zip"):
        return True
    return False


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Download item files that do not end in volview.zip")
    .modelParam("itemId", model=ItemModel, level=AccessType.READ)
    .produces(["application/zip"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the item.", 403)
)
def downloadDatasets(self, item):
    setResponseHeader("Content-Type", "application/zip")
    setContentDisposition(item["name"] + ".zip")

    def stream():
        zip = ziputil.ZipGenerator(item["name"])
        sansSessions = [
            fileEntry
            for fileEntry in ItemModel().fileList(item, subpath=False)
            if not isSessionFile(fileEntry[0])
        ]
        for path, file in sansSessions:
            for data in zip.addFile(file, path):
                yield data
        yield zip.footer()

    return stream


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Download latest *.volview.zip")
    .modelParam("itemId", model=ItemModel, level=AccessType.READ)
    .produces(["application/zip"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the item.", 403)
)
def downloadSession(self, item):
    setResponseHeader("Content-Type", "application/zip")
    setContentDisposition(item["name"] + ".zip")

    sessions = [
        fileEntry[1]
        for fileEntry in ItemModel().fileList(item, subpath=False, data=False)
        if isSessionFile(fileEntry[0])
    ]
    if len(sessions) == 0:
        raise GirderException(
            "No VolView session file found.",
            "girder.api.v1.item.volview.download-session",
        )

    sortedSessions = sorted(sessions, key=lambda file: file.get("created"))
    latestSession = sortedSessions[-1]

    return FileModel().download(latestSession, 0)


class GirderPlugin(plugin.GirderPlugin):
    DISPLAY_NAME = "VolView"
    CLIENT_SOURCE_PATH = "web_client"

    def load(self, info):
        info["apiRoot"].item.route("POST", (":itemId", "volview"), saveSession)
        info["apiRoot"].item.route("GET", (":itemId", "volview"), downloadSession)
        info["apiRoot"].item.route(
            "GET", (":itemId", "volview", "datasets"), downloadDatasets
        )
