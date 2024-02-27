import cherrypy
import errno

from girder import plugin
from girder.api.describe import Description, autoDescribeRoute
from girder.api import access
from girder.api.rest import (
    boundHandler,
    setResponseHeader,
    setContentDisposition,
)
from girder.utility.server import getApiRoot
from girder.constants import AccessType, TokenScope, SortDir

# saveSession
from girder.models.file import File as FileModel
from girder.models.upload import Upload
from girder.models.item import Item
from girder.utility import RequestBodyStream
from girder.exceptions import GirderException

# downloadDatasets
from girder.models.item import Item as ItemModel
from girder.utility import ziputil

# get config
import yaml
from girder.models.setting import Setting
from girder.models.folder import Folder
from girder import logger
from girder.models.group import Group

# server settings (from girder.cfg file probably) for proxiable endpoint below
from girder.utility import config

LARGE_IMAGE_CONFIG_FOLDER = "large_image.config_folder"


def uploadSession(model, parentId, user, size):
    # modified from girder.api.v1.file.File.initUpload
    parentType = model.__name__.lower()
    name = "session.volview.zip"
    mimeType = "application/zip"
    reference = None
    parent = model().load(id=parentId, user=user, level=AccessType.WRITE, exc=True)

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
        )
    except OSError as exc:
        if exc.errno == errno.EACCES:
            raise GirderException(
                "Failed to create upload.",
                f"girder.api.v1.{parentType}.volview_save",
            )
        raise
    if upload["size"] > 0:
        if chunk:
            return Upload().handleChunk(upload, chunk, filter=True, user=user)

        return upload
    else:
        return FileModel().filter(Upload().finalizeUpload(upload), user)


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description("Save VolView session in an item")
    .param("itemId", "The item ID", paramType="path")
    .errorResponse()
)
def saveToItem(self, itemId):
    size = int(cherrypy.request.headers.get("Content-Length"))
    if size == 0:
        raise GirderException(
            "Expected non-zero Content-Length header", "girder.api.v1.item.save-volview"
        )

    return uploadSession(ItemModel, itemId, self.getCurrentUser(), size)


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description("Save VolView session in an folder")
    .param("folderId", "The folder ID", paramType="path")
    .errorResponse()
)
def saveToFolder(self, folderId):
    size = int(cherrypy.request.headers.get("Content-Length"))
    if size == 0:
        raise GirderException(
            "Expected non-zero Content-Length header",
            "girder.api.v1.folder.volview_save",
        )
    return uploadSession(Folder, folderId, self.getCurrentUser(), size)


SESSION_ZIP_EXTENSION = ".volview.zip"


def isSessionFile(path):
    if path.endswith(SESSION_ZIP_EXTENSION):
        return True
    return False


def isLoadableData(path):
    if isSessionFile(path):
        return False
    if path.endswith("volview_config.yaml"):
        return False
    return True


# Deprecated, use downloadManifest
@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Download zip of item files that do not end in volview.zip")
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
            if isLoadableData(fileEntry[0])
        ]
        for path, file in sansSessions:
            for data in zip.addFile(file, path):
                yield data
        yield zip.footer()

    return stream


@access.public(scope=TokenScope.DATA_READ, cookie=True)
@boundHandler
@autoDescribeRoute(
    Description("Download a file with option to proxy.")
    .modelParam("id", model=FileModel, level=AccessType.READ)
    .param("name", "The name of the file.  This is ignored.", paramType="path")
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied on the parent folder.", 403)
)
def downloadProxiableFile(self, file, name):
    proxyRequest = config.getConfig().get("volview", {}).get("proxy_assetstores", True)
    return FileModel().download(file, headers=not proxyRequest)


def makeFileDownloadUrl(fileModel):
    """
    Given a file model, return a download URL for the file.
    :param fileModel: the file model.
    :type fileModel: dict
    :returns: the download URL.
    """
    # Lead with a slash to make the URI relative to origin
    fileUrl = "/".join(
        (
            "",
            getApiRoot(),
            "file",
            str(fileModel["_id"]),
            "proxiable",
            fileModel["name"],
        )
    )
    return fileUrl


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description(
        "Download JSON listing item file download URIs that do not end in volview.zip"
    )
    .modelParam("itemId", model=ItemModel, level=AccessType.READ)
    .produces(["application/json"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the item.", 403)
)
def downloadManifest(self, item):
    filesNoVolViewZips = [
        fileEntry
        for fileEntry in ItemModel().fileList(item, subpath=False, data=False)
        if isLoadableData(fileEntry[0])
    ]
    fileUrls = [
        {"url": makeFileDownloadUrl(fileEntry[1]), "name": fileEntry[1]["name"]}
        for fileEntry in filesNoVolViewZips
    ]
    fileManifest = {"resources": fileUrls}
    return fileManifest


def getFileList(model, id):
    folder = model().load(id, force=True, exc=True)
    filesNoVolViewZips = [
        fileEntry for fileEntry in model().fileList(folder, subpath=False, data=False)
    ]
    return filesNoVolViewZips


def getFiles(model, ids):
    if len(ids) == 0:
        return []
    idList = ids.split(",")
    fileLists = [getFileList(model, id) for id in idList]
    # flatten
    files = [file for fileList in fileLists for file in fileList]
    return files


def sameLevelSessionFile(fileEntry):
    # if file name matches the item name, there is no / in the path
    # example: itemName == session.volview.zip and fileName == session.volview.zip, then path == session.volview.zip
    # example: itemName == "session.volview.zip (1)" and fileName == session.volview.zip, then path == session.volview.zip (1)/session.volview.zip
    paths = fileEntry[0].split("/")
    rootPath = paths[0]
    itemNameIncludesSessionZip = rootPath.find(SESSION_ZIP_EXTENSION) != -1
    directChildSessionZip = len(paths) <= 2 and itemNameIncludesSessionZip
    return directChildSessionZip and isSessionFile(fileEntry[0])


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Download JSON with file download URIs")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("folders", "Folder IDs.")
    .param("items", "Item IDs.")
    .produces(["application/json"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the folder.", 403)
)
def downloadResourceManifest(self, folder, folders, items):
    files = []
    if not folders and not items:
        # all files in folder (unless volview.zip is found as direct child)
        filesInFolder = [
            fileEntry
            for fileEntry in Folder().fileList(folder, subpath=False, data=False)
        ]
        sessions = [
            fileEntry for fileEntry in filesInFolder if sameLevelSessionFile(fileEntry)
        ]
        if len(sessions) > 0:
            # load latest session
            sortedSessions = sorted(sessions, key=lambda file: file[1].get("created"))
            latestSession = sortedSessions[-1]
            files = [latestSession]
        else:
            files = [file for file in filesInFolder if isLoadableData(file[0])]
    else:
        # selected files
        itemFiles = getFiles(ItemModel, items)
        if len(itemFiles) == 1 and isSessionFile(itemFiles[0][0]):
            # if selected one session.volview.zip item, load it
            files = itemFiles
        else:
            files = getFiles(Folder, folders) + itemFiles
            files = [file for file in files if isLoadableData(file[0])]

    fileUrls = [
        {"url": makeFileDownloadUrl(fileEntry[1]), "name": fileEntry[1]["name"]}
        for fileEntry in files
    ]
    configUrl = "/".join(
        (
            "",
            getApiRoot(),
            "folder",
            str(folder["_id"]),
            "volview_config",
            ".volview_config.yaml",
        )
    )
    fileUrls.append({"url": configUrl, "name": "config.json"})
    fileManifest = {"resources": fileUrls}
    return fileManifest


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


def _mergeDictionaries(a, b):
    """
    Merge two dictionaries recursively.  If the second dictionary (or any
    sub-dictionary) has a special key, value of '__all__': True, the updated
    dictionary only contains values from the second dictionary and excludes
    the __all__ key.

    :param a: the first dictionary.  Modified.
    :param b: the second dictionary that gets added to the first.
    :returns: the modified first dictionary.
    """
    if b.get("__all__") is True:
        a.clear()
    for key in b:
        if isinstance(a.get(key), dict) and isinstance(b[key], dict):
            _mergeDictionaries(a[key], b[key])
        elif key != "__all__" or b[key] is not True:
            a[key] = b[key]
    return a


def adjustConfigForUser(config, user):
    """
    Given the current user, adjust the config so that only relevant and
    combined values are used.  If the root of the config dictionary contains
    "access": {"user": <dict>, "admin": <dict>}, the base values are updated
    based on the user's access level.  If the root of the config contains
    "group": {<group-name>: <dict>, ...}, the base values are updated for
    every group the user is a part of.

    The order of update is groups in C-sort alphabetical order followed by
    access/user and then access/admin as they apply.

    :param config: a config dictionary.
    """
    if not isinstance(config, dict):
        return config
    if isinstance(config.get("groups"), dict):
        groups = config.pop("groups")
        if user:
            for group in Group().find(
                {"_id": {"$in": user["groups"]}}, sort=[("name", SortDir.ASCENDING)]
            ):
                if isinstance(groups.get(group["name"]), dict):
                    config = _mergeDictionaries(config, groups[group["name"]])
    if isinstance(config.get("access"), dict):
        accessList = config.pop("access")
        if user and isinstance(accessList.get("user"), dict):
            config = _mergeDictionaries(config, accessList["user"])
        if user and user.get("admin") and isinstance(accessList.get("admin"), dict):
            config = _mergeDictionaries(config, accessList["admin"])
    return config


# Modified from https://github.com/girder/large_image/blob/aa1dc05665944e87eb9cb8553085221fab16ae92/girder/girder_large_image/__init__.py#L434-L483
def yamlConfigFile(folder, name, user, addConfig):
    """
    Get a resolved named config file based on a folder and user.

    :param folder: a Girder folder model.
    :param name: the name of the config file.
    :param user: the user that the response if adjusted for.
    :returns: either None if no config file, or a yaml record.
    """
    last = False
    while folder:
        item = Item().findOne({"folderId": folder["_id"], "name": name})
        if item:
            for file in Item().childFiles(item):
                if file["size"] > 10 * 1024**2:
                    logger.info("Not loading %s -- too large" % file["name"])
                    continue
                with FileModel().open(file) as fptr:
                    config = yaml.safe_load(fptr)
                    if isinstance(config, list) and len(config) == 1:
                        config = config[0]
                    # combine and adjust config values based on current user
                    if (
                        isinstance(config, dict)
                        and "access" in config
                        or "group" in config
                    ):
                        config = adjustConfigForUser(config, user)
                    if addConfig and isinstance(config, dict):
                        config = _mergeDictionaries(config, addConfig)
                    if (
                        not isinstance(config, dict)
                        or config.get("__inherit__") is not True
                    ):
                        return config
                    config.pop("__inherit__")
                    addConfig = config
        if last:
            break
        if folder["parentCollection"] != "folder":
            if folder["name"] != ".config":
                folder = Folder().findOne(
                    {
                        "parentId": folder["parentId"],
                        "parentCollection": folder["parentCollection"],
                        "name": ".config",
                    }
                )
            else:
                last = "setting"
            if not folder or last == "setting":
                folderId = Setting().get(LARGE_IMAGE_CONFIG_FOLDER)
                if not folderId:
                    break
                folder = Folder().load(folderId, force=True)
                last = True
        else:
            folder = Folder().load(folder["parentId"], user=user, level=AccessType.READ)
    return addConfig


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler()
@autoDescribeRoute(
    Description("Get a VolView config file.")
    .notes(
        "Wraps large image yaml_config endpoint and inserts more properties. "
        "This walks up the chain of parent folders until the file is found.  "
        "If not found, the .config folder in the parent collection or user is "
        "checked.\n\nAny yaml file can be returned.  If the top-level is a "
        'dictionary and contains keys "access" or "groups" where those are '
        "dictionaries, the returned value will be modified based on the "
        'current user.  The "groups" dictionary contains keys that are group '
        "names and values that update the main dictionary.  All groups that "
        "the user is a member of are merged in alphabetical order.  If a key "
        'and value of "\\__all\\__": True exists, the replacement is total; '
        'otherwise it is a merge.  If the "access" dictionary exists, the '
        '"user" and "admin" subdictionaries are merged if a calling user is '
        "present and if the user is an admin, respectively (both get merged "
        "for admins)."
    )
    .modelParam("itemId", model=ItemModel, level=AccessType.READ)
    .param("name", "The name of the file.", paramType="path")
    .produces(["application/json"])
    .errorResponse()
)
def getConfigFile(self, item, name):
    folderId = item["folderId"]
    user = self.getCurrentUser()
    folder = Folder().load(folderId, user=user, level=AccessType.READ)
    baseConfig = {"dataBrowser": {"hideSampleData": True}}
    config = yamlConfigFile(folder, name, user, baseConfig)
    return config


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler()
@autoDescribeRoute(
    Description("Get a VolView config file.")
    .notes(
        "Wraps large image yaml_config endpoint and inserts more properties. "
        "This walks up the chain of parent folders until the file is found.  "
        "If not found, the .config folder in the parent collection or user is "
        "checked.\n\nAny yaml file can be returned.  If the top-level is a "
        'dictionary and contains keys "access" or "groups" where those are '
        "dictionaries, the returned value will be modified based on the "
        'current user.  The "groups" dictionary contains keys that are group '
        "names and values that update the main dictionary.  All groups that "
        "the user is a member of are merged in alphabetical order.  If a key "
        'and value of "\\__all\\__": True exists, the replacement is total; '
        'otherwise it is a merge.  If the "access" dictionary exists, the '
        '"user" and "admin" subdictionaries are merged if a calling user is '
        "present and if the user is an admin, respectively (both get merged "
        "for admins)."
    )
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("name", "The name of the file.", paramType="path")
    .produces(["application/json"])
    .errorResponse()
)
def getFolderConfigFile(self, folder, name):
    user = self.getCurrentUser()
    baseConfig = {"dataBrowser": {"hideSampleData": True}}
    config = yamlConfigFile(folder, name, user, baseConfig)
    return config


class GirderPlugin(plugin.GirderPlugin):
    DISPLAY_NAME = "VolView"
    CLIENT_SOURCE_PATH = "web_client"

    def load(self, info):
        info["apiRoot"].item.route("POST", (":itemId", "volview"), saveToItem)
        info["apiRoot"].item.route("GET", (":itemId", "volview"), downloadSession)
        # volview/datasets is deprecated.  Use volview/manifest instead.
        info["apiRoot"].item.route(
            "GET", (":itemId", "volview", "datasets"), downloadDatasets
        )
        info["apiRoot"].file.route(
            "GET", (":id", "proxiable", ":name"), downloadProxiableFile
        )
        info["apiRoot"].item.route(
            "GET", (":itemId", "volview", "manifest"), downloadManifest
        )
        info["apiRoot"].item.route(
            "GET", (":itemId", "volview", "config", ":name"), getConfigFile
        )
        info["apiRoot"].folder.route(
            "GET", (":folderId", "volview_manifest"), downloadResourceManifest
        )
        info["apiRoot"].folder.route(
            "GET", (":folderId", "volview_config", ":name"), getFolderConfigFile
        )
        info["apiRoot"].folder.route("POST", (":folderId", "volview"), saveToFolder)
