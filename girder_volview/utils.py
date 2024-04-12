from datetime import datetime
from girder.utility.server import getApiRoot
from girder.constants import AccessType

SESSION_ZIP_EXTENSION = ".volview.zip"

# https://github.com/Kitware/VolView/blob/main/src/io/mimeTypes.ts
LOADABLE_EXTENSIONS = (
    # VolView app
    ".json",
    ".zip",
    ".vti",
    ".vtp",
    ".stl",
    # @itk-wasm/image-io
    ".bmp",
    ".dcm",
    ".gipl",
    ".gipl.gz",
    ".hdf5",
    ".jpg",
    ".jpeg",
    ".iwi",
    ".iwi.cbor",
    ".iwi.cbor.zst",
    ".lsm",
    ".mnc",
    ".mnc.gz",
    ".mnc2",
    ".mgh",
    ".mgz",
    ".mgh.gz",
    ".mha",
    ".mhd",
    ".mrc",
    ".nia",
    ".nii",
    ".nii.gz",
    ".hdr",
    ".nrrd",
    ".nhdr",
    ".png",
    ".pic",
    ".tif",
    ".tiff",
    ".vtk",
    ".isq",
    ".aim",
    ".fdf",
)

LOADABLE_MIMES = (
    "application/vnd.unknown.vti",
    "application/vnd.unknown.vtp",
    "model/stl",
    "application/dicom",
    "application/zip",
    "application/json",
    "application/vnd.unknown.gipl",
    "application/x-hdf5",
    "image/jpeg",
    "application/vnd.unknown.lsm",
    "application/vnd.unknown.minc",
    "application/vnd.unknown.mgh",
    "application/vnd.unknown.metaimage",
    "application/vnd.unknown.mrc",
    "application/vnd.unknown.nifti-1",
    "application/vnd.unknown.nrrd",
    "image/png",
    "application/vnd.unknown.biorad",
    "image/tiff",
    "application/vnd.unknown.vtk",
    "application/vnd.unknown.scanco",
    "application/vnd.unknown.fdf",
    # Girder manual uploads are application/octet-stream
    "application/octet-stream",
)


def isSessionItem(item):
    if item and SESSION_ZIP_EXTENSION in item["name"]:
        return True
    return False


def isSessionFile(file):
    name = file.get("name")
    if name.endswith(SESSION_ZIP_EXTENSION):
        return True
    return False


def isLoadableImage(file):
    if isSessionFile(file):
        return False
    name = file.get("name")
    knownExtension = name.endswith(LOADABLE_EXTENSIONS)
    mimeType = file.get("mimeType")
    # mimeType can be None when imported via S3 asset store
    knownMime = mimeType in LOADABLE_MIMES or mimeType is None
    return knownExtension or knownMime


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


def filesToManifest(files, folderId):
    fileUrls = [
        {"url": makeFileDownloadUrl(fileEntry[1]), "name": fileEntry[1]["name"]}
        for fileEntry in files
    ]
    configUrl = "/".join(
        (
            "",
            getApiRoot(),
            "folder",
            str(folderId),
            "volview_config",
            ".volview_config.yaml",
        )
    )
    fileUrls.append({"url": configUrl, "name": "config.json"})
    fileManifest = {"resources": fileUrls}
    return fileManifest


def sameLevelSessionFile(fileEntry):
    # if file name matches the item name, then Item.fileList has no / in the path
    # example: itemName == session.volview.zip and fileName == session.volview.zip, then path == session.volview.zip
    # example: itemName == "session.volview.zip (1)" and fileName == session.volview.zip, then path == session.volview.zip (1)/session.volview.zip
    paths = fileEntry[0].split("/")
    rootPath = paths[0]
    itemNameIncludesSessionZip = rootPath.find(SESSION_ZIP_EXTENSION) != -1
    directChildSessionZip = len(paths) <= 2 and itemNameIncludesSessionZip
    return directChildSessionZip and isSessionFile(fileEntry[1])


def singleVolViewZipOrImageFiles(fileEntries):
    sessions = [
        fileEntry for fileEntry in fileEntries if sameLevelSessionFile(fileEntry)
    ]
    if sessions:
        # load latest session
        newestSession = max(sessions, key=lambda file: file[1].get("created"))
        return [newestSession]
    else:
        return [fileEntry for fileEntry in fileEntries if isLoadableImage(fileEntry[1])]


def idStringToIdList(idString):
    if len(idString) == 0:
        return []
    return idString.split(",")


def getFiles(model, docs):
    fileLists = [model().fileList(doc, subpath=False, data=False) for doc in docs]
    # flatten
    files = [file for fileList in fileLists for file in fileList]
    return files


def loadModels(user, model, docIds, level=AccessType.READ):
    return [model().load(id, level=level, user=user) for id in docIds]


def normalizeLinkedResources(linkedResources):
    if not linkedResources:
        return {"folders": [], "items": []}
    folders = linkedResources.get("folders", [])
    items = linkedResources.get("items", [])
    return {"folders": folders, "items": items}


def getLinkedResources(item):
    resources = item.get("meta", {}).get("linkedResources", {})
    return normalizeLinkedResources(resources)


def matchesSelectionSet(folders, items, item):
    foldersA = set(folders or [])
    itemsA = set(items or [])
    itemResources = getLinkedResources(item)
    foldersB = set(itemResources.get("folders", []))
    itemsB = set(itemResources.get("items", []))
    return foldersA == foldersB and itemsA == itemsB


def getTouchedTime(item):
    if item.get("meta").get("lastOpened"):
        dateString = item.get("meta").get("lastOpened")
        return datetime.strptime(dateString, "%Y-%m-%dT%H:%M:%S.%fZ")
    return item.get("updated") or item.get("created")


def getNewestDoc(docs):
    # filter out IDs that don't exist
    loadedDocs = [doc for doc in docs if doc]
    if not loadedDocs:
        return None
    return max(loadedDocs, key=lambda session: getTouchedTime(session))


def findNewestSession(items):
    sessions = [item for item in items if isSessionItem(item)]
    return getNewestDoc(sessions)
