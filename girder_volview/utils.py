from datetime import datetime, timezone
from girder import logger
from girder.utility.server import getApiRoot
from girder.constants import AccessType
from girder.models.folder import Folder
from girder.models.item import Item

SESSION_ZIP_EXTENSION = ".volview.zip"
SESSION_JSON_EXTENSION = ".volview.json"
SESSION_EXTENSIONS = (SESSION_ZIP_EXTENSION, SESSION_JSON_EXTENSION)

# Item-metadata marker stamped on a job-output file's item at upload (Chunk 19,
# D5). Job results take the JOB path only: a marked file stays durable in the
# folder (re-fetched via the job) but is filtered OUT of the launch manifest, so
# VolView's native `loadSegmentations` convention never also grabs it (that
# carries no `source` tag, so tier-2 could not dedup against a double-apply).
# Same exclusion site as session zips (`isLoadableImage`).
JOB_OUTPUT_META_KEY = "volviewJobOutput"

SAFE_NAME_MAX = 80
SESSION_NAME_MAX = SAFE_NAME_MAX * 3
# Suffix-matched against filter keys so both 'meta.dicom.PatientID' and
# 'dicom.PatientID' get the same canonical position.
PREFERRED_FILTER_SUFFIXES = (
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
)


def safeNameComponent(value):
    safe = "".join(
        ch if ch.isalnum() or ch in ".-_" else "_" for ch in str(value)
    )
    return safe.strip("._")[:SAFE_NAME_MAX]


def _filterKeyPriority(key):
    keyStr = str(key)
    for index, suffix in enumerate(PREFERRED_FILTER_SUFFIXES):
        if keyStr.endswith(suffix):
            return (0, index, keyStr)
    return (1, 0, keyStr)


def _promoteFilterToList(value):
    """Normalize dict-or-list filter input to a list of dicts.
    Returns None for non-conforming values.
    """
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        return value
    return None


def sessionNameFromFilter(linkedFilter, extension):
    filtersList = _promoteFilterToList(linkedFilter)
    if not filtersList:
        return f"session{extension}"
    sortedFilters = sorted(
        filtersList,
        key=lambda d: tuple(sorted(d.items())),
    )
    parts = []
    seen = set()
    for filterDict in sortedFilters:
        for key in sorted(filterDict, key=_filterKeyPriority):
            component = safeNameComponent(filterDict[key])
            if component and component not in seen:
                parts.append(component)
                seen.add(component)
    if not parts:
        return f"session{extension}"
    joined = ".".join(parts)
    if len(joined) > SESSION_NAME_MAX:
        joined = joined[:SESSION_NAME_MAX].rstrip(".")
    return f"session.{joined}{extension}"

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
)


def isSessionItem(item):
    return item and any(ext in item["name"] for ext in SESSION_EXTENSIONS)


def isSessionFile(file):
    return file.get("name").endswith(SESSION_EXTENSIONS)


def isTiffFile(file):
    return (
        file.get("name").endswith((".tif", ".tiff"))
        or file.get("mimeType") == "image/tiff"
    )


def isDicomFile(file):
    return (
        file.get("name").endswith(".dcm") or file.get("mimeType") == "application/dicom"
    )


def isLoadableFile(file, user=None):
    if isTiffFile(file) or isDicomFile(file):
        item = Item().load(file.get("itemId"), user=user, level=AccessType.READ)
        if item is None:
            return False
        if isTiffFile(file):
            return not item.get("largeImage")
        if item.get("meta", {}).get("dicom", {}).get("Modality", "") == "SM":
            return False

    if file.get("name").endswith(LOADABLE_EXTENSIONS):
        return True

    return file.get("mimeType") in LOADABLE_MIMES


def isJobOutputItem(item):
    """Whether an item holds a job-output file (marked at upload — Chunk 19)."""
    return bool((item or {}).get("meta", {}).get(JOB_OUTPUT_META_KEY))


def isJobOutputFile(file, user=None):
    """Whether ``file``'s parent item is a marked job output (Chunk 19, D5).

    Job outputs are excluded from the launch manifest (results take the job path
    only) while staying durable in the folder. Best-effort: an absent/unreadable
    parent item is treated as not-a-job-output (fail toward showing the file —
    the scene-tag idempotency still guards against a double-apply).
    """
    itemId = file.get("itemId")
    if not itemId:
        return False
    item = Item().load(itemId, user=user, level=AccessType.READ, exc=False)
    return isJobOutputItem(item)


def isLoadableImage(file, user=None):
    if isSessionFile(file):
        return False
    if isJobOutputFile(file, user):
        return False
    return isLoadableFile(file, user)


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


def _toIso(value):
    """Serialize a datetime to an ISO-8601 UTC string, or ``None``.

    The facade's neutral instants (the session watermark, a job's ``finishedAt``)
    travel the wire as ISO-8601 UTC strings so the client compares them as UTC
    instants — no client clock, no timezone ambiguity (D5). Girder stores naive
    UTC datetimes, so a naive value is tagged UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def sessionSavedAtFromFiles(files):
    """The server-side save instant of a session zip among ``files``, or ``None``.

    ``singleVolViewZipOrImageFiles`` selects EITHER one session zip OR the loose
    loadable images (never both), so a session file in the manifest's file set
    means this launch restores that session; its file-record ``created`` is the
    session's server-side save instant — the D5 watermark comparand
    (``finishedAt > sessionSavedAt``). Absent (no session) → ``None`` and the
    field is omitted, so the client attaches every re-discovered result (MVP
    parity). Server clock only; nothing new is stored (no state-file change).
    """
    for fileEntry in files:
        if isSessionFile(fileEntry[1]):
            return _toIso(fileEntry[1].get("created"))
    return None


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
    # Surface the restored session's save instant as the tier-2 watermark
    # (Chunk 19, D5). Present iff a session zip is selected; nothing new stored.
    savedAt = sessionSavedAtFromFiles(files)
    if savedAt is not None:
        fileManifest["sessionSavedAt"] = savedAt
    return fileManifest


def sameLevelSessionFile(fileEntry):
    # if file name matches the item name, then Item.fileList has no / in the path
    # example: itemName == session.volview.zip and fileName == session.volview.zip, then path == session.volview.zip
    # example: itemName == "session.volview.zip (1)" and fileName == session.volview.zip, then path == session.volview.zip (1)/session.volview.zip
    paths = fileEntry[0].split("/")
    rootPath = paths[0]
    itemNameIncludesSession = any(ext in rootPath for ext in SESSION_EXTENSIONS)
    directChildSession = len(paths) <= 2 and itemNameIncludesSession
    return directChildSession and isSessionFile(fileEntry[1])


def filterLinkedSessionItemIds(fileEntries):
    sessionItemIds = {
        fileEntry[1].get("itemId")
        for fileEntry in fileEntries
        if sameLevelSessionFile(fileEntry) and fileEntry[1].get("itemId")
    }
    if not sessionItemIds:
        return set()
    matches = Item().find(
        {
            "_id": {"$in": list(sessionItemIds)},
            "meta.linkedResources.filter": {"$exists": True},
        },
        fields=["_id"],
    )
    return {item["_id"] for item in matches}


def singleVolViewZipOrImageFiles(fileEntries, user=None, includeFilterLinkedSessions=True):
    fileEntries = list(fileEntries)
    excludedItemIds = (
        set() if includeFilterLinkedSessions
        else filterLinkedSessionItemIds(fileEntries)
    )
    sessions = [
        fileEntry for fileEntry in fileEntries
        if sameLevelSessionFile(fileEntry)
        and fileEntry[1].get("itemId") not in excludedItemIds
    ]
    if sessions:
        # load latest session
        newestSession = max(sessions, key=lambda file: file[1].get("created"))
        return [newestSession]
    else:
        return [fileEntry for fileEntry in fileEntries if isLoadableImage(fileEntry[1], user)]


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
    result = {"folders": folders, "items": items}
    if 'filter' in linkedResources:
        result['filter'] = linkedResources['filter']
    return result


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
    if item.get("meta", {}).get("lastOpened"):
        dateString = item.get("meta", {}).get("lastOpened")
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


def getFilteredFiles(folder, filters):
    """
    Given a folder and a set of item filter criteria, find all files that are
    in items in the folder or any of its sub-folders that match the filter.
    Accepts a single filter dict or a list of dicts (OR-unioned).
    """
    filtersList = _promoteFilterToList(filters) or []
    if len(filtersList) > 1:
        itemMatch = {'$or': filtersList}
    elif filtersList:
        itemMatch = filtersList[0]
    else:
        itemMatch = {}
    folderId = folder['_id']
    pipeline = [
        {'$match': {'_id': folderId}},
        {'$graphLookup': {
            'from': 'folder',
            'connectFromField': '_id',
            'connectToField': 'parentId',
            'depthField': '_depth',
            'as': 'folders',
            'startWith': '$_id',
        }},
        {'$addFields': {'folders': {
            '$concatArrays': [[{'_id': '$_id'}], '$folders']
        }}},
        {'$unwind': '$folders'},
        {'$replaceRoot': {'newRoot': '$folders'}},
        {'$lookup': {
            'from': 'item',
            'localField': '_id',
            'foreignField': 'folderId',
            'as': 'items'
        }},
        {'$unwind': '$items'},
        {'$replaceRoot': {'newRoot': '$items'}},
        {'$match': itemMatch},
        {'$lookup': {
            'from': 'file',
            'localField': '_id',
            'foreignField': 'itemId',
            'as': 'files'
        }},
        {'$unwind': '$files'},
        {'$replaceRoot': {'newRoot': '$files'}},
    ]
    logger.info('Filtering pipeline: %s', pipeline)
    filesInFolder = list(Folder().collection.aggregate(pipeline))
    return filesInFolder


def filterMatchesSession(rowFilter, sessionFilter):
    """A row filter matches a session filter when they have the same set of
    filter dicts. Both arguments may be a single dict or a list of dicts; a
    dict is treated as a single-element list. Order is irrelevant; cardinality
    must match and each dict must be content-equal to a counterpart on the
    other side.
    """
    rowList = _promoteFilterToList(rowFilter)
    sessionList = _promoteFilterToList(sessionFilter)
    if rowList is None or sessionList is None:
        return False

    def canon(d):
        return tuple(sorted(d.items()))

    return sorted(map(canon, rowList)) == sorted(map(canon, sessionList))


def getFilteredSessionFile(folder, filters, user):
    candidates = list(Item().find(
        {
            'folderId': folder['_id'],
            'meta.linkedResources.filter': {'$exists': True},
        },
        fields=['_id', 'name', 'meta.linkedResources', 'meta.lastOpened', 'updated', 'created'],
    ))
    matches = [
        item for item in candidates
        if filterMatchesSession(filters, item.get('meta', {}).get('linkedResources', {}).get('filter'))
    ]
    item = findNewestSession(matches)
    if not item:
        return None
    item = Item().load(item['_id'], user=user, level=AccessType.READ)
    files = singleVolViewZipOrImageFiles(
        Item().fileList(item, subpath=False, data=False), user=user,
    )
    return files
