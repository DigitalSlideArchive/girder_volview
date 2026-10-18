from itertools import islice
import re

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
from .utils import (
    JOB_OUTPUT_FOLDER_META_KEY, LOADABLE_EXTENSIONS, LOADABLE_MIMES,
    SESSION_EXTENSIONS, TRANSIENT_STAGED_META_KEY, isLoadableImage, isSessionFile)

VOLVIEW_LOADABLE_FILE_LIMIT = 1000
VOLVIEW_LOADABLE_BATCH_SIZE = 128

_LOADABLE_NAME_RE = re.compile( "(" + "|".join(
    re.escape(ext) for ext in
    tuple(LOADABLE_EXTENSIONS) + tuple(SESSION_EXTENSIONS)) + ")$").pattern


def _loadCandidateItems(itemIds):
    return {
        item["_id"]: item for item in Item().find(
            {"_id": {"$in": list(itemIds)}},
            fields={
                "_id": 1,
                "folderId": 1,
                "largeImage": 1,
                "meta.dicom.Modality": 1,
                "meta." + TRANSIENT_STAGED_META_KEY: 1,
            },
        )
    }


def _cacheReadableFolders(items, user, folderCache):
    folderIds = {
        item["folderId"] for item in items
        if item.get("folderId") and str(item["folderId"]) not in folderCache
    }
    if not folderIds:
        return
    folders = {
        str(folder["_id"]): folder
        for folder in Folder().findWithPermissions(
            query={"_id": {"$in": list(folderIds)}},
            user=user,
            level=AccessType.READ,
            fields={"_id": 1, "meta." + JOB_OUTPUT_FOLDER_META_KEY: 1},
        )
    }
    folderCache.update({str(fid): folders.get(str(fid)) for fid in folderIds})


def _cacheCandidateParents(files, user, itemCache, folderCache):
    itemIds = {
        file["itemId"] for file in files
        if file.get("itemId") and not isSessionFile(file)
    } - itemCache.keys()
    if not itemIds:
        return
    items = _loadCandidateItems(itemIds)
    _cacheReadableFolders(items.values(), user, folderCache)
    # Items inherit their parent folder's ACL; cache missing and denied parents
    # too so the predicate never falls back to singleton loads.
    for itemId in itemIds:
        item = items.get(itemId)
        readable = item and folderCache.get(str(item.get("folderId"))) is not None
        itemCache[itemId] = item if readable else None


def _loadableFolderFiles(files, user, itemCache, folderCache):
    files = iter(files)
    # Check the first candidate alone; batch remaining parents if needed.
    batchSize = 1
    while batch := list(islice(files, batchSize)):
        batchSize = VOLVIEW_LOADABLE_BATCH_SIZE
        _cacheCandidateParents(batch, user, itemCache, folderCache)
        yield from batch


def hasLoadableFile(files, user=None, *, batchParents=False):
    # Mirror what the launch manifest would actually resolve: a session file
    # opens through (restore), and anything else must be a loadable image that
    # is not working data (job outputs, transient staged inputs) — otherwise
    # the Open-in-VolView button would launch an empty viewer.
    itemCache = {}
    folderCache = {}
    if batchParents:
        files = _loadableFolderFiles(files, user, itemCache, folderCache)
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
    # Start with one candidate for early success; align later cursor batches
    # with the parent metadata batches to limit read-ahead after a rejection.
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
            {"$match": {
        "$and": [
            # Exclude attached files; they should never trigger
            {"$nor": [{"attachedToId": {"$exists": True}}]},
            # More filters to reduce total files examines and increase
            # likelihood of files being loadable
            {
                "$or": [
                    {"name": {"$regex": _LOADABLE_NAME_RE}},
                    {"mimeType": {"$in": list(LOADABLE_MIMES)}},
                ]
            },
        ]
    }},
            # Cap this to make the query manageable
            {"$limit": VOLVIEW_LOADABLE_FILE_LIMIT},
        ],
        batchSize=1,
    ).batch_size(VOLVIEW_LOADABLE_BATCH_SIZE)
    loadable = hasLoadableFile(files, user=self.getCurrentUser(), batchParents=True)
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
