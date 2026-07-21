import cherrypy

from girder import plugin
from girder.api.describe import Description, autoDescribeRoute
from girder.api import access
from girder.api.rest import (
    boundHandler,
    setResponseHeader,
    setContentDisposition,
)
from girder.constants import AccessType, TokenScope

from girder.models.file import File
from girder.models.item import Item

from girder.models.folder import Folder

# server settings (from girder.cfg file probably) for proxiable endpoint below
from girder.utility import config

from .dicom import setupEventHandlers
from .backend import addBackendRoutes
from .backend.launch import (
    downloadManifest,
    downloadResourceManifest,
    getFolderConfigFile,
    saveToItem,
    saveToFolder,
)
from .utils import isLoadableImage, isSessionFile


def hasLoadableFile(files, user=None):
    # Mirror what the launch manifest would actually resolve: a session file
    # opens through (restore), and anything else must be a loadable image that
    # is not working data (job outputs, transient staged inputs) — otherwise
    # the Open-in-VolView button would launch an empty viewer.
    itemCache = {}
    folderCache = {}
    for fileEntry in files:
        file = fileEntry[1] if isinstance(fileEntry, tuple) else fileEntry
        if isSessionFile(file) or isLoadableImage(file, user, itemCache, folderCache):
            return True
    return False


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description(
        "Check if item has files VolView can load.  If so, return {loadable:true}."
    )
    .modelParam("itemId", model=Item, level=AccessType.READ)
    .produces(["application/json"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the folder.", 403)
)
def volViewLoadableItem(self, item):
    files = Item().fileList(item, subpath=False, data=False)
    loadable = hasLoadableFile(files, user=self.getCurrentUser())
    return {"loadable": loadable}


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description(
        "Check if folder has files VolView can load.  If so, return {loadable:true}."
    )
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .produces(["application/json"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the folder.", 403)
)
def volViewLoadableFolder(self, folder):
    # The aggregation below replaces:
    #   files = Folder().fileList(
    #       folder, user=self.getCurrentUser(), subpath=False, data=False
    #   )
    # excepting that it just returns a cursor that yields files, not an
    # iterator that yields a tuple of (path, file).  The aggregation is much
    # faster, as it only takes one database roundtrip, rather than one per
    # folder and one per item.
    files = Folder().collection.aggregate(
        [
            {"$match": {"_id": folder["_id"]}},
            {
                "$graphLookup": {
                    "from": "folder",
                    "startWith": folder["_id"],
                    "connectFromField": "_id",
                    "connectToField": "parentId",
                    "as": "__children",
                }
            },
            {
                "$lookup": {
                    "from": "folder",
                    "localField": "_id",
                    "foreignField": "_id",
                    "as": "__self",
                }
            },
            {
                "$project": {
                    "__children": {
                        "$concatArrays": [
                            "$__self",
                            "$__children",
                        ]
                    }
                }
            },
            {"$unwind": {"path": "$__children"}},
            {"$replaceRoot": {"newRoot": "$__children"}},
            {
                "$match": Folder().permissionClauses(
                    self.getCurrentUser(), level=AccessType.READ
                )
            },
            {
                "$lookup": {
                    "from": "item",
                    "let": {"fid": "$_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$$fid", "$folderId"]}}},
                        {"$project": {"_id": 1}},
                    ],
                    "as": "__items",
                }
            },
            {
                "$lookup": {
                    "from": "file",
                    "localField": "__items._id",
                    "foreignField": "itemId",
                    "as": "__files",
                }
            },
            {"$unwind": "$__files"},
            {"$replaceRoot": {"newRoot": "$__files"}},
        ]
    )
    loadable = hasLoadableFile(files, user=self.getCurrentUser())
    return {"loadable": loadable}


@access.public(scope=TokenScope.DATA_READ, cookie=True)
@boundHandler
@autoDescribeRoute(
    Description("Download a file with option to proxy.")
    .modelParam("id", model=File, level=AccessType.READ)
    .param("name", "The name of the file. This is ignored.", paramType="path")
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied on the parent folder.", 403)
)
def downloadProxiableFile(self, file, name):
    proxyRequest = config.getConfig().get("volview", {}).get("proxy_assetstores", True)

    # below modified from girder.api.v1.file.download
    rangeRequest = cherrypy.request.headers.get("Range")
    if rangeRequest and file.get("size") is None:
        # Ensure the file size is updated
        File().updateSize(file)

    rangeHeader = cherrypy.lib.httputil.get_ranges(rangeRequest, file.get("size", 0))

    if rangeRequest:
        if not rangeHeader:
            # cherrypy found something wrong with range request headers in get_ranges
            cherrypy.response.status = 416
            cherrypy.response.headers["Content-Range"] = f"bytes */{file['size']}"
            return ""
        # Only support the first range
        offset, endByte = rangeHeader[0]
    else:
        offset = 0
        endByte = None

    # to get s3_assetstore_adapter to proxy s3, we set headers to False, but that
    # also suppresses Girder's default download headers. Set safe ones explicitly
    # so a proxied file always downloads (attachment) with an inert content type
    # and can never render inline in a browser. Transparent to the engine's fetch,
    # which reads the response body regardless of these headers.
    if proxyRequest:
        setResponseHeader("Content-Type", "application/octet-stream")
        setContentDisposition(file["name"])

    # to have a correct partial response, fill in headers and status code
    if proxyRequest and (
        offset > 0 or (endByte is not None and endByte < file["size"])
    ):
        cherrypy.response.status = 206
        cherrypy.response.headers["Accept-Ranges"] = "bytes"
        if endByte is None:
            endByte = file["size"]
        # endByte is non-inclusive, so set Content-Range accordingly
        cherrypy.response.headers["Content-Range"] = (
            f"bytes {offset}-{endByte - 1}/{file['size']}"
        )
        cherrypy.response.headers["Content-Length"] = str(endByte - offset)
    elif proxyRequest:
        cherrypy.response.headers["Accept-Ranges"] = "bytes"
        cherrypy.response.headers["Content-Length"] = str(file["size"])

    return File().download(
        file, offset=offset, endByte=endByte, headers=not proxyRequest
    )


class GirderPlugin(plugin.GirderPlugin):
    DISPLAY_NAME = "VolView"
    CLIENT_SOURCE_PATH = "web_client"

    def load(self, info):
        plugin.getPlugin("large_image").load(info)
        setupEventHandlers()

        info["apiRoot"].item.route(
            "GET", (":itemId", "volview_loadable"), volViewLoadableItem
        )
        info["apiRoot"].folder.route(
            "GET", (":folderId", "volview_loadable"), volViewLoadableFolder
        )
        info["apiRoot"].item.route("GET", (":itemId", "volview"), downloadManifest)
        info["apiRoot"].folder.route(
            "GET", (":folderId", "volview"), downloadResourceManifest
        )
        # Session-zip save: item-scoped stuffs the zip into the item,
        # folder-scoped creates a new session.volview.zip item. Each returns a
        # resumeUrl the client repoints its urls= at, so a later F5 reloads the
        # just-made save.
        info["apiRoot"].item.route("POST", (":itemId", "volview"), saveToItem)
        info["apiRoot"].folder.route("POST", (":folderId", "volview"), saveToFolder)
        info["apiRoot"].file.route(
            "GET", (":id", "proxiable", ":name"), downloadProxiableFile
        )
        info["apiRoot"].folder.route(
            "GET", (":folderId", "volview_config", ":name"), getFolderConfigFile
        )
        addBackendRoutes(info)
