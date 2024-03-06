from datetime import datetime
from girder.utility.server import getApiRoot
from girder.constants import AccessType

SESSION_ZIP_EXTENSION = ".volview.zip"


def isSessionItem(item):
    if item and SESSION_ZIP_EXTENSION in item["name"]:
        return True
    return False


def isSessionFile(path):
    if path.endswith(SESSION_ZIP_EXTENSION):
        return True
    return False


def isLoadableImage(path):
    if isSessionFile(path):
        return False
    if path.endswith("volview_config.yaml"):
        return False
    return True


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
    return directChildSessionZip and isSessionFile(fileEntry[0])


def singleVolViewZipOrImageFiles(files):
    sessions = [fileEntry for fileEntry in files if sameLevelSessionFile(fileEntry)]
    if sessions:
        # load latest session
        newestSession = max(sessions, key=lambda file: file[1].get("created"))
        return [newestSession]
    else:
        return [file for file in files if isLoadableImage(file[0])]


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
