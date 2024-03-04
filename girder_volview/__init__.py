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
from girder.constants import AccessType, TokenScope, SortDir

from girder.models.file import File
from girder.models.upload import Upload
from girder.utility import RequestBodyStream
from girder.exceptions import GirderException

from girder.models.item import Item
from girder.utility import ziputil

# used by get config
import yaml
from girder.models.setting import Setting
from girder.models.folder import Folder
from girder import logger
from girder.models.group import Group

# server settings (from girder.cfg file probably) for proxiable endpoint below
from girder.utility import config

from .utils import (
    isSessionItem,
    isLoadableImage,
    filesToManifest,
    singleVolViewZipOrImageFiles,
    idStringToIdList,
    normalizeLinkedResources,
    loadModels,
    getFiles,
    findNewestSession,
    getLinkedResources,
    matchesSelectionSet,
    getNewestDoc,
    getTouchedTime,
)

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
        return File().filter(Upload().finalizeUpload(upload), user)


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

    return uploadSession(Item, itemId, self.getCurrentUser(), size)


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description("Save VolView session in an folder")
    .param("folderId", "The folder ID", paramType="path")
    .jsonParam(
        "metadata",
        "A JSON object containing the metadata keys to add to the item.",
    )
    .errorResponse()
)
def saveToFolder(self, folderId, metadata):
    user = self.getCurrentUser()
    size = int(cherrypy.request.headers.get("Content-Length"))
    if size == 0:
        raise GirderException(
            "Expected non-zero Content-Length header",
            "girder.api.v1.folder.volview_save",
        )
    fileDic = uploadSession(Folder, folderId, user, size)
    # Ensure next downloadResourcesManifest request for will find this
    # session.volview.zip as the freshest session that matches the selection set:
    # If there are session.volview.zip items in linked items,
    # use its metadata.linkedResources as this item's metadata.linkedResources
    linkedResources = metadata["linkedResources"]
    linkedItems = normalizeLinkedResources(linkedResources)["items"]
    selectedItems = loadModels(user, Item, linkedItems)
    newestSelectedSession = findNewestSession(selectedItems)
    if newestSelectedSession:
        # LinkedResources points to volview.zip.  Change saved volview.zip linkedResources
        # to match selected linkedResources so we find most recent volview.zip next manifest
        # request for those linkedResources.
        metadata = {"linkedResources": getLinkedResources(newestSelectedSession)}

    item = Item().load(fileDic["itemId"], user=user, level=AccessType.WRITE, exc=True)
    Item().setMetadata(item, metadata)
    return fileDic


# Deprecated, use downloadManifest
@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Download zip of item files that do not end in volview.zip")
    .modelParam("itemId", model=Item, level=AccessType.READ)
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
            for fileEntry in Item().fileList(item, subpath=False)
            if isLoadableImage(fileEntry[0])
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
    .modelParam("id", model=File, level=AccessType.READ)
    .param("name", "The name of the file.  This is ignored.", paramType="path")
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied on the parent folder.", 403)
)
def downloadProxiableFile(self, file, name):
    proxyRequest = config.getConfig().get("volview", {}).get("proxy_assetstores", True)
    return File().download(file, headers=not proxyRequest)


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description(
        "Download JSON listing item file download URIs that do not end in volview.zip"
    )
    .modelParam("itemId", model=Item, level=AccessType.READ)
    .produces(["application/json"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the item.", 403)
)
def downloadManifest(self, item):
    allFiles = list(Item().fileList(item, subpath=False, data=False))
    files = singleVolViewZipOrImageFiles(allFiles)
    return filesToManifest(files, item["folderId"])


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
    user = self.getCurrentUser()
    folders = idStringToIdList(folders)
    items = idStringToIdList(items)
    files = []
    if not folders and not items:
        # All files in folder (unless volview.zip is found as direct child)
        filesInFolder = [
            fileEntry
            for fileEntry in Folder().fileList(folder, subpath=False, data=False)
        ]
        files = singleVolViewZipOrImageFiles(filesInFolder)
    else:
        # else load selected files
        selectedItems = loadModels(user, Item, items)
        # If any selected items are session.volview.zip,
        # find freshest and change selection set to match its linkedResources.
        newestSelectedSession = findNewestSession(selectedItems)
        if newestSelectedSession:
            linkedResources = getLinkedResources(newestSelectedSession)
            folders = linkedResources["folders"]
            items = linkedResources["items"]

        # Find session.volview.zips that match selection set
        sessionItems = [
            item for item in Folder().childItems(folder) if isSessionItem(item)
        ]
        matchingSessionItems = [
            session
            for session in sessionItems
            if matchesSelectionSet(folders, items, session)
        ]
        latestSession = getNewestDoc(matchingSessionItems)
        # compare touched time of session with max touched time of selected items/folders
        selectedFolders = loadModels(user, Folder, folders)
        latestSelectedDoc = getNewestDoc(selectedFolders + selectedItems)
        if (
            latestSession
            and latestSelectedDoc
            and getTouchedTime(latestSession) >= getTouchedTime(latestSelectedDoc)
        ):
            # session touched time is newer than selected items/folders so load it
            files = singleVolViewZipOrImageFiles(
                Item().fileList(latestSession, subpath=False, data=False)
            )
        else:
            # Load selected folders and items excluding child session.volview.zip and .volview_config.yaml
            files = getFiles(Folder, selectedFolders) + getFiles(Item, selectedItems)
            files = [file for file in files if isLoadableImage(file[0])]
    return filesToManifest(files, folder["_id"])


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
                with File().open(file) as fptr:
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
        info["apiRoot"].item.route("GET", (":itemId", "volview"), downloadManifest)
        info["apiRoot"].folder.route(
            "GET", (":folderId", "volview"), downloadResourceManifest
        )
        info["apiRoot"].file.route(
            "GET", (":id", "proxiable", ":name"), downloadProxiableFile
        )
        info["apiRoot"].folder.route(
            "GET", (":folderId", "volview_config", ":name"), getFolderConfigFile
        )
        info["apiRoot"].folder.route("POST", (":folderId", "volview"), saveToFolder)
        info["apiRoot"].item.route("POST", (":itemId", "volview"), saveToItem)
        # volview/datasets is deprecated.  Use GET {folder|item}/volview instead.
        info["apiRoot"].item.route(
            "GET", (":itemId", "volview", "datasets"), downloadDatasets
        )
