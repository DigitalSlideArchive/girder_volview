from datetime import datetime
from girder.utility.server import getApiRoot
from girder.models.item import Item

SESSION_ZIP_EXTENSION = ".volview.zip"


def isSessionItem(item):
    if SESSION_ZIP_EXTENSION in item["name"]:
        return True
    return False


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
        return [file for file in files if isLoadableData(file[0])]


def getFileList(model, id, user):
    doc = model().load(id, user=user, exc=True)
    return model().fileList(doc, subpath=False, data=False)


def idStringToIdList(idString):
    if len(idString) == 0:
        return set([])
    return set(idString.split(","))


def getFiles(model, idList, user):
    fileLists = [getFileList(model, id, user) for id in idList]
    # flatten
    files = [file for fileList in fileLists for file in fileList]
    return files


def loadItems(user, itemIds):
    return [Item().load(itemId, user=user) for itemId in itemIds]


def normalizeLinkedResources(linkedResources):
    if not linkedResources:
        return {"folders": set(), "items": set()}
    folders = linkedResources.get("folders", set())
    items = linkedResources.get("items", set())
    return {"folders": folders, "items": items}


def getLinkedResources(item):
    linkedResources = item.get("meta", {}).get("linkedResources")
    return normalizeLinkedResources(linkedResources)


def matchesSelectionSet(folders, items, sessionItem):
    linkedResources = getLinkedResources(sessionItem)
    linkedFolders = linkedResources.get("folders")
    linkedItems = linkedResources.get("items")
    return folders == linkedFolders and items == linkedItems

    for folder in folders:
        if not folder in linkedResources.get("folders", []):
            return False
    for item in items:
        if not item in linkedResources.get("items", []):
            return False
    return True


def getTouchedTime(item):
    if item.get("meta").get("lastOpened"):
        dateString = item.get("meta").get("lastOpened")
        return datetime.strptime(dateString, "%Y-%m-%dT%H:%M:%S.%fZ")
    return item.get("updated") or item.get("created")


def getNewestSession(sessions):
    return max(sessions, key=lambda session: getTouchedTime(session))


def findNewestSession(items):
    selectedSessions = [item for item in items if isSessionItem(item)]
    if selectedSessions:
        return getNewestSession(selectedSessions)
    return []
