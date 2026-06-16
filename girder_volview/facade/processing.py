"""Processing facade — provider config + slicer-cli proxy for VolView.

Translates VolView-native processing requests into `slicer_cli_web` calls and
projects Girder jobs back into the VolView provider contract.

SourceRef plumbing:
- Refs are provider-owned opaque strings. For this Girder provider they are
  raw Girder model ids.
- On every resolution the facade re-loads the document with the *user's*
  permissions (`AccessType.READ` for inputs, `AccessType.WRITE` for output
  folders). The Girder permission check is the security boundary.
"""

import copy
import json

from bson.objectid import ObjectId
from girder import logger
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import boundHandler, setRawResponse, setResponseHeader
from girder.constants import AccessType, TokenScope
from girder.exceptions import RestException
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item

# ---------------------------------------------------------------------------
# SourceRef — provider-owned opaque handle
# ---------------------------------------------------------------------------

def encodeSourceRef(fileId=None, itemId=None, folderId=None):
    """Return the Girder id VolView should pass back as an opaque sourceRef."""
    if fileId is not None:
        return str(fileId)
    if itemId is not None:
        return str(itemId)
    if folderId is not None:
        return str(folderId)
    raise RestException("Cannot create sourceRef without a Girder id")


def _stripTypedSourceRef(ref, expectedType):
    """Accept raw ids and optional `girder:<type>:<id>` refs."""
    if not isinstance(ref, str) or not ref:
        raise RestException("Malformed sourceRef")
    prefix = f"girder:{expectedType}:"
    if ref.startswith(prefix):
        return ref[len(prefix):]
    return ref


def _looksLikeSourceRef(value, expectedType):
    if not isinstance(value, str):
        return False
    try:
        candidate = _stripTypedSourceRef(value, expectedType)
    except RestException:
        return False
    return ObjectId.is_valid(candidate)


def resolveSourceRefToFile(ref, user):
    """Load the referenced file with the user's READ permission."""
    fileId = _stripTypedSourceRef(ref, "file")
    f = File().load(fileId, user=user, level=AccessType.READ, exc=True)
    return f


def resolveSourceRefToFolder(ref, user, level=AccessType.WRITE):
    folderId = _stripTypedSourceRef(ref, "folder")
    folder = Folder().load(folderId, user=user, level=level, exc=True)
    return folder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _loadedSourcesForFolder(folder, user):
    sources = []
    for item in Folder().childItems(folder, user=user, limit=50):
        files = list(Item().childFiles(item, limit=1))
        if not files:
            continue
        f = files[0]
        sources.append({
            "datasetId": str(item["_id"]),
            "name": item["name"],
            "sourceRef": encodeSourceRef(
                fileId=f["_id"], itemId=item["_id"], folderId=folder["_id"]
            ),
        })
    return sources


def _providerBaseUrl(folder):
    return f"/api/v1/folder/{folder['_id']}/volview_processing"


def _providerConfigForFolder(folder, user):
    loadedSources = _loadedSourcesForFolder(folder, user)
    activeSourceRef = loadedSources[0]["sourceRef"] if loadedSources else None
    return {
        "id": "girder-slicer-cli",
        "label": "Analysis",
        "protocol": "slicer-cli",
        "baseUrl": _providerBaseUrl(folder),
        "auth": "same-origin",
        "context": {
            "activeSourceRef": activeSourceRef,
            "loadedSources": loadedSources,
        },
    }


def buildProcessingConfigBlock(folder, user):
    return {"providers": [_providerConfigForFolder(folder, user)]}


# ---------------------------------------------------------------------------
# slicer_cli_web bridge
# ---------------------------------------------------------------------------

def _slicerCliAvailable():
    try:
        import slicer_cli_web  # noqa: F401
        return True
    except ImportError:
        return False


def _listCliItems(user):
    """Return CLIItem instances visible to the user."""
    from slicer_cli_web.models import CLIItem
    return list(CLIItem.findAllItems(user))


def _findCliItem(taskId, user):
    """Resolve a taskId to a CLIItem. taskId is the underlying Item._id."""
    from slicer_cli_web.models import CLIItem
    item = CLIItem.find(taskId, user)
    return item


def _cliItemToSummary(cliItem):
    return {
        "id": str(cliItem._id),
        "title": cliItem.name,
        "description": cliItem.item.get("description", ""),
        "dockerImage": cliItem.image,
    }


# Compound extensions we want to preserve as a single suffix.
_COMPOUND_EXTENSIONS = (
    ".nii.gz", ".tar.gz", ".mgh.gz", ".hdr.gz", ".mnc.gz",
    ".iwi.cbor.zst", ".iwi.cbor",
)


def _splitExt(name):
    """Like os.path.splitext but recognizes radiology compound extensions."""
    lower = name.lower()
    for ext in _COMPOUND_EXTENSIONS:
        if lower.endswith(ext):
            return name[: -len(ext)], name[-len(ext):]
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]


def _defaultExtensionForOutput(out):
    """Pick a sensible extension when the CLI didn't declare one."""
    if out["tag"] == "image":
        return ".nii.gz"
    return ".dat"


def _outputExtension(out):
    """Return the first declared fileExtension, or a tag-based default."""
    raw = out.get("fileExtensions") or ""
    for ext in raw.split(","):
        ext = ext.strip()
        if ext:
            return ext if ext.startswith(".") else "." + ext
    return _defaultExtensionForOutput(out)


def _candidateOutputName(inputBase, cliName, paramName, ext):
    """Build a deterministic candidate name; uniquifying is a separate step."""
    base = (inputBase or "output").strip(". ")
    cli = (cliName or "task").strip(". ")
    return f"{base}.{cli}.{paramName}{ext}"


def _uniquifyItemName(folder, candidate):
    """Append ` (N)` to the base until the name doesn't collide in the folder."""
    base, ext = _splitExt(candidate)
    name = candidate
    suffix = 2
    while Item().findOne({"folderId": folder["_id"], "name": name}) is not None:
        name = f"{base} ({suffix}){ext}"
        suffix += 1
        if suffix > 999:  # safety
            break
    return name


def _firstSourceRefFile(values, user):
    """Resolve the first SourceRef-looking value to a file doc, if any."""
    for v in (values or {}).values():
        if _looksLikeSourceRef(v, "file"):
            try:
                return resolveSourceRefToFile(v, user)
            except Exception as exc:
                logger.debug("Skipping unresolved sourceRef candidate: %s", exc)
                continue
    return None


def _autofillOutputs(values, cli_xml, cli_name, user, folder):
    """Auto-generate unique names for any output params the client didn't fill in.

    Mutates and returns `values`. Output param values become
    `ProcessingOutputRequest`-style dicts: `{"name": "<unique>", ...}`.
    """
    outputs = _parseCliOutputs(cli_xml or "")
    if not outputs:
        return values

    inputFile = _firstSourceRefFile(values, user)
    inputBase, _ = _splitExt((inputFile or {}).get("name") or "")
    if not inputBase:
        inputBase = "output"

    for out in outputs:
        existing = values.get(out["name"])
        if isinstance(existing, dict) and existing.get("name"):
            continue
        ext = _outputExtension(out)
        candidate = _candidateOutputName(inputBase, cli_name, out["name"], ext)
        unique = _uniquifyItemName(folder, candidate)
        new_value = {"name": unique}
        if isinstance(existing, dict):
            new_value.update({k: v for k, v in existing.items() if k != "name"})
        values[out["name"]] = new_value
    return values


def _parseCliOutputs(xmlText):
    """Parse `<image channel=output>` / `<file channel=output>` from XML.

    Returns a list of dicts:
        [{name, tag, isLabel, fileExtensions}]
    Used to pick result files out of Job._original_params after success.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xmlText)
    except ET.ParseError:
        return []
    outputs = []
    for param in root.iter():
        channelEl = param.find("channel")
        if channelEl is None or (channelEl.text or "").strip() != "output":
            continue
        if param.tag not in {"image", "file"}:
            continue
        nameEl = param.find("name")
        if nameEl is None or not nameEl.text:
            continue
        outputs.append({
            "name": nameEl.text.strip(),
            "tag": param.tag,
            "isLabel": param.get("type") == "label",
            "fileExtensions": (param.get("fileExtensions") or "").lower(),
        })
    return outputs


def _readLabelsSidecar(fileDoc):
    """Read a small JSON sidecar listing per-label segment descriptors.

    Returns a list like `[{"value": 1, "name": "...", "color": [r,g,b,a]}, ...]`
    or `None` if the file isn't a parseable JSON list of labels.
    """
    if (fileDoc.get("size") or 0) > 256 * 1024:
        return None  # not our sidecar
    try:
        chunks = File().download(fileDoc, headers=False)
        raw = b"".join(chunks() if callable(chunks) else chunks)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    cleaned = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if "value" not in entry or "name" not in entry:
            continue
        color = entry.get("color")
        if not isinstance(color, list) or len(color) not in (3, 4):
            continue
        if len(color) == 3:
            color = list(color) + [255]
        cleaned.append({
            "value": int(entry["value"]),
            "name": str(entry["name"]),
            "color": [int(c) for c in color],
        })
    return cleaned or None


def _translateValuesToSlicerParams(values, doc_xml, user, folder):
    """Translate VolView values payload to slicer_cli_web's form-encoded params.

    - SourceRef inputs → fileId
    - ProcessingOutputRequest outputs → name + name_folder (output goes back
      to the launching folder by default)
    - Scalars → str(value)
    """
    params = {}
    for paramName, value in (values or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            params[paramName] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            params[paramName] = str(value)
        elif isinstance(value, str):
            # Could be a Girder file sourceRef or a plain string.
            if _looksLikeSourceRef(value, "file"):
                try:
                    f = resolveSourceRefToFile(value, user)
                    params[paramName] = str(f["_id"])
                    continue
                except Exception as exc:
                    logger.debug(
                        "Leaving unresolved sourceRef-like value as string: %s", exc
                    )
            params[paramName] = value
        elif isinstance(value, dict) and "name" in value:
            # ProcessingOutputRequest
            params[paramName] = value["name"]
            outFolderRef = value.get("folderRef")
            if outFolderRef:
                outFolder = resolveSourceRefToFolder(outFolderRef, user)
            else:
                outFolder = folder
            params[f"{paramName}_folder"] = str(outFolder["_id"])
        elif isinstance(value, list):
            params[paramName] = ",".join(str(v) for v in value)
        else:
            params[paramName] = str(value)
    return params


def _projectJobStatus(job):
    """Convert Girder Job status to ProcessingJobStatus."""
    from girder_jobs.constants import JobStatus
    state_map = {
        JobStatus.INACTIVE: "pending",
        JobStatus.QUEUED: "pending",
        JobStatus.RUNNING: "running",
        JobStatus.SUCCESS: "success",
        JobStatus.ERROR: "error",
        JobStatus.CANCELED: "cancelled",
    }
    state = state_map.get(job.get("status"), "pending")
    out = {"jobId": str(job["_id"]), "state": state}
    if state == "error":
        log = job.get("log") or []
        if isinstance(log, list):
            tail = "".join(log[-20:])
        else:
            tail = str(log)[-2000:]
        out["errorTail"] = tail
    progress = job.get("progress") or {}
    if progress.get("total") and progress.get("current") is not None:
        try:
            out["progress"] = float(progress["current"]) / float(progress["total"])
        except (TypeError, ZeroDivisionError):
            pass
    return out


def _collectJobResults(job, user):
    """Find result files based on Job._original_params + CLI XML."""
    from slicer_cli_web.models import CLIItem
    results = []
    original_params = job.get("_original_params") or {}
    if not original_params:
        return results

    # Look up the CLI XML from job metadata if possible.
    cli_xml = None
    cli_id = job.get("_original_path")
    # _original_path is a folder restBasePath (e.g. ".../<image>"). Easier:
    # look up CLIItem by name + path.
    name = job.get("_original_name")
    if name:
        from girder_jobs.models.job import Job as JobModel  # noqa: F401
        # Try to find the matching CLIItem by name.
        try:
            for c in CLIItem.findAllItems(user):
                if c.name == name:
                    cli_xml = c.xml
                    break
        except Exception:
            logger.exception("Failed to look up CLIItem for job results")

    outputs = _parseCliOutputs(cli_xml) if cli_xml else []
    if not outputs:
        return results

    # Pass 1: resolve each declared output to its uploaded file document.
    resolved = []
    for out in outputs:
        outName = out["name"]
        if outName not in original_params:
            continue
        fileName = original_params[outName]
        folderId = original_params.get(f"{outName}_folder")
        if not fileName or not folderId:
            continue
        try:
            folder = Folder().load(
                folderId, user=user, level=AccessType.READ, exc=False
            )
            if not folder:
                continue
            item = Item().findOne({
                "folderId": folder["_id"], "name": fileName,
            })
            if not item:
                continue
            fileDoc = next(iter(Item().childFiles(item, limit=1)), None)
            if not fileDoc:
                continue
        except Exception:
            logger.exception("Failed to resolve job output file")
            continue
        resolved.append({"out": out, "fileDoc": fileDoc})

    # Pass 2: find any JSON labels sidecars and pair them with labelmap outputs.
    sidecars = []
    for entry in list(resolved):
        out = entry["out"]
        fileDoc = entry["fileDoc"]
        name = (fileDoc.get("name") or "").lower()
        if out["tag"] == "file" and (
            name.endswith(".json") or ".labels.json" in name
        ):
            labels = _readLabelsSidecar(fileDoc)
            if labels:
                sidecars.append(labels)
                resolved.remove(entry)
    # For now, pair-by-position: a sidecar attaches to the first labelmap output.
    labelmap_entries = [
        e for e in resolved
        if e["out"]["tag"] == "image" and e["out"]["isLabel"]
    ]

    # Pass 3: project each remaining file into ProcessingResult.
    for entry in resolved:
        out = entry["out"]
        fileDoc = entry["fileDoc"]
        role = "segmentGroup" if out["isLabel"] else None
        url = (
            f"/api/v1/file/{fileDoc['_id']}/proxiable/"
            f"{fileDoc['name']}"
        )
        result = {
            "id": str(fileDoc["_id"]),
            "name": fileDoc["name"],
            "url": url,
            **({"role": role} if role else {}),
            "mimeType": fileDoc.get("mimeType"),
            "size": fileDoc.get("size"),
        }
        if (
            role == "segmentGroup"
            and sidecars
            and entry in labelmap_entries
        ):
            # First labelmap gets the first sidecar.
            idx = labelmap_entries.index(entry)
            if idx < len(sidecars):
                result["segments"] = sidecars[idx]
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get the VolView processing provider config for a folder.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .produces(["application/json"])
)
def getProviderConfig(self, folder):
    user = self.getCurrentUser()
    return {"providers": [_providerConfigForFolder(folder, user)]}


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("List processing tasks available for a folder.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .produces(["application/json"])
)
def listTasks(self, folder):
    user = self.getCurrentUser()
    tasks = []
    if user and _slicerCliAvailable():
        try:
            tasks.extend([_cliItemToSummary(c) for c in _listCliItems(user)])
        except Exception:
            logger.exception("Failed to list slicer_cli_web items")
    return tasks


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get the Slicer CLI XML for a task.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("taskId", "The task identifier.", paramType="path")
)
def getTaskXml(self, folder, taskId):
    user = self.getCurrentUser()
    setResponseHeader("Content-Type", "application/xml")
    setRawResponse()
    if not _slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=404)
    cliItem = _findCliItem(taskId, user)
    if not cliItem:
        raise RestException("Unknown taskId", code=404)
    return cliItem.xml


@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description("Submit a processing task.")
    .modelParam("folderId", model=Folder, level=AccessType.WRITE)
    .param("taskId", "The task identifier.", paramType="path")
    .jsonParam(
        "body",
        "Submission payload: { values: { paramName: ProcessingValue, ... } }",
        paramType="body",
        required=False,
    )
)
def runTask(self, folder, taskId, body):
    user = self.getCurrentUser()
    values = (body or {}).get("values", {}) if isinstance(body, dict) else {}

    if not _slicerCliAvailable():
        raise RestException("slicer_cli_web is not installed", code=500)

    cliItem = _findCliItem(taskId, user)
    if not cliItem:
        raise RestException("Unknown taskId", code=404)

    from girder.models.token import Token
    from slicer_cli_web.rest_slicer_cli import genHandlerToRunDockerCLI

    # Auto-generate (unique) output filenames so the user never has to. Any
    # output param missing from `values` gets a fresh name keyed off the input
    # file + CLI name + parameter name + extension.
    values = _autofillOutputs(dict(values), cliItem.xml, cliItem.name, user, folder)

    # Translate VolView values to slicer_cli_web params.
    params = _translateValuesToSlicerParams(values, cliItem.xml, user, folder)
    logger.info(
        "[volview_processing] runTask folder=%s task=%s params=%s",
        folder["_id"], taskId, params,
    )

    handler = genHandlerToRunDockerCLI(cliItem)
    token = Token().createToken(user=user)
    # Take a copy so the handler can mutate freely.
    job_obj = handler.subHandler(cliItem, copy.deepcopy(params), user, token)
    job_doc = job_obj.job if hasattr(job_obj, "job") else job_obj
    return {"jobId": str(job_doc["_id"])}


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get job status.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJob(self, folder, jobId):
    user = self.getCurrentUser()
    from girder_jobs.models.job import Job as JobModel
    job = JobModel().load(jobId, user=user, level=AccessType.READ, exc=True)
    return _projectJobStatus(job)


@access.public(cookie=True, scope=TokenScope.DATA_READ)
@boundHandler
@autoDescribeRoute(
    Description("Get job results.")
    .modelParam("folderId", model=Folder, level=AccessType.READ)
    .param("jobId", "The job identifier.", paramType="path")
    .produces(["application/json"])
)
def getJobResults(self, folder, jobId):
    user = self.getCurrentUser()
    from girder_jobs.models.job import Job as JobModel
    job = JobModel().load(jobId, user=user, level=AccessType.READ, exc=True)
    return _collectJobResults(job, user)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def addProcessingRoutes(info):
    info["apiRoot"].folder.route(
        "GET", (":folderId", "volview_processing"), getProviderConfig
    )
    info["apiRoot"].folder.route(
        "GET", (":folderId", "volview_processing", "tasks"), listTasks
    )
    info["apiRoot"].folder.route(
        "GET",
        (":folderId", "volview_processing", "tasks", ":taskId", "xml"),
        getTaskXml,
    )
    info["apiRoot"].folder.route(
        "POST",
        (":folderId", "volview_processing", "tasks", ":taskId", "run"),
        runTask,
    )
    info["apiRoot"].folder.route(
        "GET",
        (":folderId", "volview_processing", "jobs", ":jobId"),
        getJob,
    )
    info["apiRoot"].folder.route(
        "GET",
        (":folderId", "volview_processing", "jobs", ":jobId", "results"),
        getJobResults,
    )
