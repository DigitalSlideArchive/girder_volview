"""Launch / compose / config / save handlers for the ordinary VolView viewer.

Each launch gesture has one meaning: raw checked picks ALWAYS open fresh; a
checked session item opens through to exactly that session; a filter gesture
resumes its newest matching session; a bare folder-open resumes the folder's
newest unfiltered session. A save returns a ``resumeUrl`` the client uses for
subsequent reloads (F5); the save target itself stays launch-provided, so
folder saves mint a new ``session.volview.zip`` item per save.
"""

import copy
import errno

import cherrypy
import yaml

from girder import logger
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import boundHandler
from girder.constants import AccessType, TokenScope, SortDir
from girder.exceptions import GirderException, RestException
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.group import Group
from girder.models.item import Item
from girder.models.setting import Setting
from girder.models.upload import Upload
from girder.utility import RequestBodyStream
from girder.utility.server import getApiRoot

from .config import buildProcessingConfigBlock
from ..utils import (
    SESSION_ZIP_EXTENSION,
    isJobOutputFolderItem,
    isLaunchFile,
    isLoadableImage,
    primeLoadableImageCaches,
    filesToManifest,
    singleVolViewZipOrImageFiles,
    getFilteredFiles,
    getFilteredSessionFile,
    getFiles,
    getLinkedResources,
    idStringToIdList,
    findNewestSession,
    loadModels,
    normalizeLinkedResources,
    sessionNameFromFilter,
)

LARGE_IMAGE_CONFIG_FOLDER = "large_image.config_folder"

BASE_CONFIG = {
    "io": {
        "segmentGroupExtension": "seg",
        "segmentGroupSaveFormat": "nii.gz",
        "layerExtension": "layer",
    },
    "disabledViewTypes": ["3D", "Oblique"],
    "layouts": {
        "Axial Coronal Sagittal": {
            "direction": "row",
            "items": [
                "axial",
                {"direction": "column", "items": ["coronal", "sagittal"]},
            ],
        },
        "Axial Only": [["axial"]],
    },
}


def uploadSession(model, parentId, user, size, metadata=None):
    # modified from girder.api.v1.file.File.initUpload
    parentType = model.__name__.lower()
    name = f"session{SESSION_ZIP_EXTENSION}"
    try:
        # Metadata comes from the client; don't fail the save if the shape
        # isn't what we expect.
        linkedFilter = (metadata or {}).get("linkedResources", {}).get("filter")
        name = sessionNameFromFilter(linkedFilter, SESSION_ZIP_EXTENSION)
    except Exception:
        pass

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
            ) from exc
        raise
    if upload["size"] > 0:
        if chunk:
            return Upload().handleChunk(upload, chunk, filter=True, user=user)

        return upload
    else:
        return File().filter(Upload().finalizeUpload(upload), user)


def _saveResponse(sessionItemId):
    """The save response — a SINGLE field: the session's load URL.

    The VolView client stays opaque to Girder ids: it never learns the item id,
    only the ``resumeUrl`` (``item/:id/volview``), which it repoints ONLY its
    reload (``urls=``) at. So a later F5 reloads exactly this save, while the
    save target (``save=``) stays launch-provided — a folder-scoped save mints
    a new ``session.volview.zip`` item on every save.
    """
    return {"resumeUrl": f"/{getApiRoot()}/item/{sessionItemId}/volview"}


def _uploadWholeSession(model, parentId, user, errorIdentifier, metadata=None):
    """Upload the session zip in one shot; 400 unless it finalized into a File.

    Only a finalized File carries ``itemId``. A resumable/partial upload (a
    processor content-type, or a body shorter than the declared Content-Length)
    returns the raw Upload doc with no ``itemId``; the single-shot save contract
    can't continue, so fail with a clean 400 rather than report a success F5
    would contradict by restoring the previous zip (or a KeyError -> 500 and an
    orphaned item).
    """
    try:
        size = int(cherrypy.request.headers.get("Content-Length"))
    except (TypeError, ValueError):
        # Absent (e.g. Transfer-Encoding: chunked) or non-integer header:
        # the same clean rejection as an empty body, not an int() 500.
        size = 0
    if size == 0:
        raise GirderException(
            "Expected non-zero Content-Length header", errorIdentifier
        )
    fileDic = uploadSession(model, parentId, user, size, metadata)
    if "itemId" not in fileDic:
        raise RestException(
            "Session save must upload the whole zip in one request.", code=400
        )
    return fileDic


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description("Save VolView session in an item")
    .param("itemId", "The item ID", paramType="path")
    .errorResponse()
)
def saveToItem(self, itemId):
    _uploadWholeSession(
        Item, itemId, self.getCurrentUser(), "girder.api.v1.item.save-volview"
    )
    # The session file is stuffed into this same item, so its own manifest URL
    # is both the save target and the F5 reload target.
    return _saveResponse(itemId)


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
    # jsonParam yields whatever the client sent, so `metadata` can be a list or
    # scalar. Coerce before the upload -- same "don't fail the save on an
    # unexpected shape" stance as uploadSession, and a guard placed after the
    # upload would raise only once the zip is stored, 500ing on an orphan item.
    if not isinstance(metadata, dict):
        metadata = {}
    # Rebase this save's linkedResources onto the newest already-saved session in
    # the selection set: a save from a checked-session open would otherwise stamp
    # linkedResources={items:[S]} instead of S's own lineage. Load-bearing for
    # filter sessions — a save from a checked FILTER-session open must inherit
    # the filter link so the filter row resumes this newest save and the bare
    # folder-open keeps excluding it.
    #
    # Resolved BEFORE the upload: a malformed linkedResources (truthy non-object)
    # or an unloadable id must 4xx with nothing stored, not raise once the zip has
    # already finalized into a session item that a folder-open would then pick as
    # the newest session to restore.
    rawLinked = metadata.get("linkedResources")
    linkedResources = normalizeLinkedResources(
        rawLinked if isinstance(rawLinked, dict) else None
    )
    selectedItems = loadModels(user, Item, linkedResources["items"])
    newestSelectedSession = findNewestSession(selectedItems)
    savedMetadata = metadata
    if newestSelectedSession:
        savedMetadata = {"linkedResources": getLinkedResources(newestSelectedSession)}

    fileDic = _uploadWholeSession(
        Folder, folderId, user, "girder.api.v1.folder.volview_save", metadata
    )
    item = Item().load(fileDic["itemId"], user=user, level=AccessType.WRITE, exc=True)
    try:
        Item().setMetadata(item, savedMetadata)
    except Exception:
        # An unstamped session item is still the folder's newest session, so a
        # later folder-open would restore this failed save. Drop it instead.
        Item().remove(item)
        raise
    return _saveResponse(fileDic["itemId"])


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description(
        "Download the VolView launch manifest for an item: a session.volview.zip "
        "item opens through (restore); any other item resolves to its loadable "
        "images (fresh)."
    )
    .modelParam("itemId", model=Item, level=AccessType.READ)
    .produces(["application/json"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the item.", 403)
)
def downloadManifest(self, item):
    user = self.getCurrentUser()
    # Job outputs stay durable in the folder but out of the launch manifest: a
    # direct open of an item inside a job's private output folder yields nothing.
    if isJobOutputFolderItem(item):
        return filesToManifest([], item["folderId"])
    allFiles = list(Item().fileList(item, subpath=False, data=False))
    # A session file opens through (restore); otherwise the item's raw images.
    files = singleVolViewZipOrImageFiles(
        allFiles, user=user, itemCache={item["_id"]: item}, folderCache={}
    )
    return filesToManifest(files, item["folderId"])


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description(
        "Download the VolView launch manifest for a folder / checked / filter "
        "gesture: a checked session item opens through to exactly that session "
        "(back-in-history); raw checked items/folders ALWAYS open fresh; a "
        "filter gesture resumes its newest matching session when one exists; a "
        "bare folder-open resumes the folder's newest session.volview.zip, "
        "else all its raw images. An explicit folders/items "
        "selection takes precedence over filters; filters apply only when no "
        "selection is passed, and the filtered leg returns only loadable images "
        "(transient staged inputs and job-output-folder files are excluded)."
    )
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("folders", "Folder IDs.", required=False)
    .param("items", "Item IDs.", required=False)
    .jsonParam(
        "filters",
        "Filter (dict) or filter list (array of dicts) to apply within a folder.",
        required=False,
    )
    .produces(["application/json"])
    .errorResponse("ID was invalid.")
    .errorResponse("Read access was denied for the folders or items.", 403)
)
def downloadResourceManifest(self, folder, folders, items, filters):
    user = self.getCurrentUser()
    itemCache = {}
    folderCache = {}
    folders = idStringToIdList(folders or "")
    items = idStringToIdList(items or "")
    # filters is either a dict, a list of dicts, or absent. Anything else
    # (bare scalar) is rejected here rather than 500ing in Mongo.
    if filters is not None and not isinstance(filters, (dict, list)):
        raise RestException("filters must be a JSON object or array of objects")
    # An explicit folders/items selection wins over filters: a stale/bookmarked
    # URL carrying both must load the checked resources, not silently
    # substitute the filter set.
    if folders or items:
        selectedItems = loadModels(user, Item, items)
        checkedSession = findNewestSession(selectedItems)
        if checkedSession:
            # An explicitly checked session item opens through to EXACTLY that
            # session — the back-in-history gesture. Never re-match it to a
            # newer sibling save. Filter-linked sessions get the same treatment:
            # re-entering the filter row resumes the newest, but checking an old
            # one opens exactly it.
            files = singleVolViewZipOrImageFiles(
                Item().fileList(checkedSession, subpath=False, data=False),
                user=user,
                itemCache=itemCache,
                folderCache=folderCache,
            )
            return filesToManifest(files, folder["_id"])

        # Raw checked picks ALWAYS open fresh: checking images is the "start
        # fresh" gesture, so no saved session is ever substituted. Resume
        # happens only through the other gestures — bare folder-open (newest
        # save), checking a session item (exactly that save), or re-entering a
        # filter row (newest filter save).
        selectedFolders = loadModels(user, Folder, folders)
        files = getFiles(Folder, selectedFolders) + getFiles(Item, selectedItems)
        primeLoadableImageCaches([f[1] for f in files], user, itemCache, folderCache)
        files = [
            f for f in files if isLoadableImage(f[1], user, itemCache, folderCache)
        ]
    elif filters:
        files = getFilteredSessionFile(folder, filters, user)
        # Empty is treated as no-match, not as a resolved session: a matched
        # session whose files are all unloadable would otherwise skip the fresh
        # leg and emit a manifest of nothing but config.json — a blank viewer
        # with no gesture that recovers it.
        if not files:
            files = getFilteredFiles(folder, filters)
            # The filter row owns every file it matched — no loadability gate
            # (grouped DICOM rows carry extensionless slices). Only working
            # data is excluded: transient staged inputs and session zips.
            primeLoadableImageCaches(files, user, itemCache, folderCache)
            files = [
                (None, f)
                for f in files
                if isLaunchFile(f, user, itemCache, folderCache)
            ]
    else:
        # Bare folder-open -> resume the folder's newest session.volview.zip,
        # else all its raw images. Filter-linked sessions are excluded (they are
        # only meaningful re-entered through their filter).
        filesInFolder = list(Folder().fileList(folder, subpath=False, data=False))
        files = singleVolViewZipOrImageFiles(
            filesInFolder,
            user=user,
            includeFilterLinkedSessions=False,
            itemCache=itemCache,
            folderCache=folderCache,
        )
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
                    if isinstance(config, dict) and (
                        "access" in config or "groups" in config
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
    baseConfig = copy.deepcopy(BASE_CONFIG)
    config = yamlConfigFile(folder, name, user, None) or {}
    config = _mergeDictionaries(baseConfig, config)
    # Injected dynamically rather than living in BASE_CONFIG: the providers list
    # depends on the folder being launched.
    processing = buildProcessingConfigBlock(folder)
    _mergeDictionaries(config, {"processing": processing})
    return config
