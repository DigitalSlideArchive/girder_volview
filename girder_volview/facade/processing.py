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
from girder import events, logger
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

_SERIES_REF_PREFIX = "series:"


def encodeSourceRef(fileId=None, itemId=None, folderId=None, seriesInstanceUID=None):
    """Return the opaque sourceRef VolView should pass back.

    A multi-file DICOM series encodes as ``series:<folderId>:<SeriesInstanceUID>``
    so its whole file set can be re-resolved at submit (item 3.3 — see
    `resolveSeriesSourceRefToFiles`). A single-file volume keeps the historical
    raw-id form (file id, then item, then folder). The ref stays opaque to the
    client either way: VolView core never learns it is a Girder handle.
    """
    if seriesInstanceUID is not None:
        if folderId is None:
            raise RestException(
                "Cannot create a series sourceRef without a folder id"
            )
        return f"{_SERIES_REF_PREFIX}{folderId}:{seriesInstanceUID}"
    if fileId is not None:
        return str(fileId)
    if itemId is not None:
        return str(itemId)
    if folderId is not None:
        return str(folderId)
    raise RestException("Cannot create sourceRef without a Girder id")


def decodeSeriesSourceRef(ref):
    """Parse ``series:<folderId>:<SeriesInstanceUID>`` → ``(folderId, uid)``.

    Returns ``None`` for any ref that is not a series volume handle (e.g. a raw
    file/item id), so callers can fall back to the single-file resolution path.
    """
    if not isinstance(ref, str) or not ref.startswith(_SERIES_REF_PREFIX):
        return None
    rest = ref[len(_SERIES_REF_PREFIX):]
    folderId, sep, uid = rest.partition(":")
    if not sep or not folderId or not uid:
        return None
    return folderId, uid


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

# ---------------------------------------------------------------------------
# Volume grouping (D10 part 1) — one advertised source per *volume*, not per item
#
# A 3D volume reaches Girder in no fixed layout: one file = one volume (L1), one
# item = many files = one series (L2), or one folder = many single-file items =
# one series (L3, the usual case). Emitting one source per *item* (the old shape)
# fed a multi-slice series to a job as a single slice. We instead group the
# folder's files by ``SeriesInstanceUID`` so each advertised source is a real
# volume carrying its whole file set; non-DICOM / ungroupable files stay one
# volume per file. Staging the pixels at submit is item 3.3, not here.
# ---------------------------------------------------------------------------

def _parseFileDicomTags(fileDoc):
    """Per-file DICOM tag dict (or ``None`` for non-DICOM / unreadable files).

    Used only for multi-file (L2) items, where the item's cached ``meta.dicom``
    reflects just one representative child file and so cannot order or group the
    rest. Thin wrapper over ``dicom._parseFile`` so tests can stub it.
    """
    try:
        from girder_volview.dicom import _parseFile
        return _parseFile(fileDoc)
    except Exception:
        logger.exception("Failed to parse per-file DICOM tags for volume grouping")
        return None


def _seriesUid(dicom):
    """Return a non-empty ``SeriesInstanceUID`` string, or ``None``."""
    if not isinstance(dicom, dict):
        return None
    uid = dicom.get("SeriesInstanceUID")
    if uid is None:
        return None
    uid = str(uid).strip()
    return uid or None


def _sliceSortKey(dicom):
    """Slice ordering key: ``InstanceNumber`` first, then ImagePositionPatient z.

    Numbered slices sort ahead of unnumbered ones; ties (and unnumbered slices)
    fall back to the patient-Z component. Geometry-exact reconstruction is the
    assembler's job (item 3.3, SimpleITK); this only orders the advertised set.
    """
    dicom = dicom or {}
    instance = dicom.get("InstanceNumber")
    try:
        instanceKey = (0, int(instance))
    except (TypeError, ValueError):
        instanceKey = (1, 0)
    ipp = dicom.get("ImagePositionPatient")
    posKey = 0.0
    if isinstance(ipp, (list, tuple)) and len(ipp) >= 3:
        try:
            posKey = float(ipp[2])
        except (TypeError, ValueError):
            posKey = 0.0
    return (instanceKey, posKey)


def _seriesSource(orderedEntries, uid, folderId):
    """Build one volume source from the ordered file entries of a DICOM series."""
    fileIds = [e["fileId"] for e in orderedEntries]
    if len(fileIds) == 1:
        # A single-file series (e.g. a multi-frame DICOM, L1): the one file is
        # already the whole volume, so keep the historical raw-file-id ref.
        sourceRef = encodeSourceRef(fileId=fileIds[0])
    else:
        sourceRef = encodeSourceRef(seriesInstanceUID=uid, folderId=folderId)
    descr = orderedEntries[0]["dicom"].get("SeriesDescription")
    descr = str(descr).strip() if descr else ""
    matchKey = {"kind": "series", "seriesInstanceUID": uid}
    if descr:
        matchKey["seriesDescription"] = descr
    return {
        "datasetId": orderedEntries[0]["itemId"],
        "name": descr or orderedEntries[0]["name"],
        "sourceRef": sourceRef,
        "fileIds": fileIds,
        "matchKey": matchKey,
    }


def _singleFileSource(entry):
    """Build one volume source from a lone (non-DICOM / ungroupable) file."""
    return {
        "datasetId": entry["itemId"],
        "name": entry["name"],
        "sourceRef": encodeSourceRef(fileId=entry["fileId"]),
        "fileIds": [entry["fileId"]],
        "matchKey": {"kind": "name", "name": entry["name"]},
    }


def _groupEntriesIntoVolumes(entries, folderId):
    """Pure: normalized file entries → one source per volume, in discovery order.

    DICOM entries sharing a ``SeriesInstanceUID`` collapse into a single
    slice-ordered volume; everything else stays one volume per file. The
    sourceRef remains opaque and the match key is metadata the client already
    holds, so VolView core never learns Girder.
    """
    groups = {}  # key -> {"uid": uid|None, "entries": [...]} (insertion-ordered)
    for entry in entries:
        uid = _seriesUid(entry["dicom"])
        key = ("series", uid) if uid else ("file", entry["fileId"])
        groups.setdefault(key, {"uid": uid, "entries": []})["entries"].append(entry)

    sources = []
    for group in groups.values():
        if group["uid"]:
            ordered = sorted(group["entries"], key=lambda e: _sliceSortKey(e["dicom"]))
            sources.append(_seriesSource(ordered, group["uid"], folderId))
        else:
            sources.append(_singleFileSource(group["entries"][0]))
    return sources


def _folderFileEntries(folder, user):
    """Flatten a folder's items into per-file entries with DICOM tags.

    Single-file items reuse the item's cached ``meta.dicom`` (the usual L3
    one-slice-per-item layout, already computed by ``dicom.py``). Multi-file
    items (L2: one item, many DICOM files) parse each child file because item
    metadata reflects only one representative file.
    """
    entries = []
    for item in Folder().childItems(folder, user=user, limit=0):
        if (item.get("meta") or {}).get("volviewTransient"):
            # An assembled volume staged for a job (item 3.3) is internal
            # plumbing, deleted when its job finishes — never a user source.
            continue
        files = list(Item().childFiles(item, limit=0))
        if not files:
            continue
        itemDicom = (item.get("meta") or {}).get("dicom")
        multi = len(files) > 1
        for f in files:
            tags = _parseFileDicomTags(f) if multi else itemDicom
            entries.append({
                "fileId": str(f["_id"]),
                "itemId": str(item["_id"]),
                "name": (f.get("name") or item["name"]) if multi else item["name"],
                "dicom": tags,
            })
    return entries


def _loadedSourcesForFolder(folder, user):
    return _groupEntriesIntoVolumes(
        _folderFileEntries(folder, user), str(folder["_id"])
    )


def resolveSeriesSourceRefToFiles(ref, user):
    """Resolve a ``series:<folderId>:<UID>`` ref to its ordered file documents.

    Re-runs folder grouping under the user's READ permission so submit (item
    3.3) reproduces the exact slice order the provider advertised. Each file is
    loaded with ``AccessType.READ`` — the Girder permission check is the
    security boundary. Raises if the ref is not a series ref or resolves to no
    files (e.g. the series was deleted between launch and submit).
    """
    decoded = decodeSeriesSourceRef(ref)
    if not decoded:
        raise RestException("Not a series sourceRef")
    folderId, uid = decoded
    folder = Folder().load(folderId, user=user, level=AccessType.READ, exc=True)
    matching = [
        e for e in _folderFileEntries(folder, user) if _seriesUid(e["dicom"]) == uid
    ]
    if not matching:
        raise RestException("Series sourceRef no longer resolves to any files")
    ordered = sorted(matching, key=lambda e: _sliceSortKey(e["dicom"]))
    return [
        File().load(e["fileId"], user=user, level=AccessType.READ, exc=True)
        for e in ordered
    ]


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


def _intentForOutput(out):
    """Result intent name for an output (item 3.1).

    Emitted additively alongside the legacy ``role`` using the same five-name
    v1 vocabulary the client validates (VolView ``src/processing/intents.ts``):
    a labelmap → ``attach-segment-group`` (mirrors ``role == "segmentGroup"``),
    a plain image → ``add-base-image``, any other file → ``download``. No CLI
    declares a state output yet, so ``restore-state`` has no producer here.
    """
    if out["isLabel"]:
        return "attach-segment-group"
    if out["tag"] == "image":
        return "add-base-image"
    return "download"


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


# ---------------------------------------------------------------------------
# Volume staging at submit (D10 part 2, item 3.3) — b1: assemble to one file
#
# A multi-file DICOM series sourceRef (``series:<folderId>:<UID>``, item 3.2)
# names a whole volume, but every CLI today declares an ``<image>``/``<file>``
# input that wants ONE file. So at submit we resolve the series to its ordered
# slice files, assemble them into a single geometry-correct NRRD with SimpleITK
# (``ImageSeriesReader``, which reconstructs spacing/origin/direction from the
# slices and so fixes the ``[1,1,1]`` regression — DICOM_SPACING_FIX_PLAN.md),
# upload that as a transient file in the launch folder, and bind its file id.
# Assembly runs synchronously in the Girder web process inside ``runTask``;
# SimpleITK lives in the facade environment. The transient file is deleted when
# its job reaches a terminal state (``_cleanupTransientOnJobDone``).
#
# Dispatch is on the CLI's *declared input type* (decisions.md D10): only the
# ``<image>``/``<file>`` branch (b1) is live — every CLI declares one. The
# ``<directory>`` branch (b2) is a deferred seam that raises until a CLI needs
# it. A task may opt out of assembly via a per-CLI 2D/per-slice declaration
# (escape hatch), binding a single slice instead of the whole volume.
# ---------------------------------------------------------------------------

# Slicer XML input tags that carry a single resource (a sourceRef value).
_FILE_INPUT_TAGS = {"image", "file", "directory"}
# Per-task dimensionality declarations that opt out of whole-volume assembly.
_PER_SLICE_DIMENSIONALITY = {"2d", "slice", "single", "per-slice"}


def _parseCliInputs(xmlText):
    """Map each input param name to its Slicer XML tag (image/file/directory).

    Mirrors ``_parseCliOutputs`` but for inputs (channel is absent or != output).
    Used to dispatch volume staging on the declared input type at submit.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xmlText)
    except ET.ParseError:
        return {}
    inputs = {}
    for param in root.iter():
        if param.tag not in _FILE_INPUT_TAGS:
            continue
        channelEl = param.find("channel")
        channel = (channelEl.text or "").strip() if channelEl is not None else ""
        if channel == "output":
            continue
        nameEl = param.find("name")
        if nameEl is None or not nameEl.text:
            continue
        inputs[nameEl.text.strip()] = param.tag
    return inputs


def _taskBindsSingleFile(xmlText):
    """Whether a CLI declares 2D/per-slice dimensionality (interim escape hatch).

    Whole-volume assembly is the default. A task opts into per-slice binding by
    declaring ``volview-dimensionality="2d"`` (or ``slice``/``single``) on its
    root ``<executable>`` element — a minimal per-CLI flag pending full config
    plumbing (post-MVP). No CLI declares it today, so every task assembles.
    """
    if not xmlText:
        return False
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xmlText)
    except ET.ParseError:
        return False
    declared = (root.get("volview-dimensionality") or "").strip().lower()
    return declared in _PER_SLICE_DIMENSIONALITY


def _assembleDicomToFile(orderedPaths, outPath):
    """Assemble ordered DICOM slice files into one volume written at ``outPath``.

    ``ImageSeriesReader`` reconstructs spacing/origin/direction from the slices'
    ``ImagePositionPatient`` tags — the multi-slice geometry the per-slice path
    dropped to ``[1,1,1]``. A lone file is read straight through (passthrough).
    The output format follows ``outPath``'s extension (NRRD).
    """
    import SimpleITK as sitk
    paths = list(orderedPaths)
    if len(paths) == 1:
        image = sitk.ReadImage(paths[0])
    else:
        reader = sitk.ImageSeriesReader()
        reader.SetFileNames(paths)
        image = reader.Execute()
    sitk.WriteImage(image, outPath)
    return outPath


def _assembledVolumeName(files):
    """Deterministic name for the assembled transient volume."""
    base = (files[0].get("name") if files else None) or "volume"
    base, _ = _splitExt(base)
    base = base.strip(". ") or "volume"
    return f"{base}.assembled.nrrd"


def _downloadFileTo(fileDoc, destPath):
    """Stream a Girder file's bytes to a local path."""
    chunks = File().download(fileDoc, headers=False)
    data = chunks() if callable(chunks) else chunks
    with open(destPath, "wb") as fh:
        for chunk in data:
            fh.write(chunk)
    return destPath


def _stageAssembledVolume(files, user, folder):
    """Download a series' files, assemble to NRRD, upload as a transient file.

    Returns ``(fileId, transientItemId)``. The transient item is flagged so the
    source listing skips it (``_folderFileEntries``) and the job-done handler
    deletes it (``_cleanupTransientOnJobDone``). Runs in the Girder web process.
    """
    import os
    import shutil
    import tempfile
    from girder.models.upload import Upload

    tmp = tempfile.mkdtemp(prefix="volview-assemble-")
    try:
        orderedPaths = []
        for idx, f in enumerate(files):
            # Content (not extension) drives the DICOM read; keep names unique
            # and ordered so ImageSeriesReader honors the slice order.
            local = os.path.join(tmp, f"{idx:05d}_{f.get('name') or 'slice'}")
            _downloadFileTo(f, local)
            orderedPaths.append(local)
        outName = _assembledVolumeName(files)
        outPath = os.path.join(tmp, outName)
        _assembleDicomToFile(orderedPaths, outPath)
        size = os.path.getsize(outPath)
        with open(outPath, "rb") as stream:
            fileDoc = Upload().uploadFromFile(
                stream,
                size=size,
                name=outName,
                parentType="folder",
                parent=folder,
                user=user,
                mimeType="application/x-nrrd",
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    transientItemId = fileDoc.get("itemId")
    if transientItemId:
        item = Item().load(transientItemId, force=True)
        if item:
            Item().setMetadata(item, {"volviewTransient": True})
    return str(fileDoc["_id"]), (str(transientItemId) if transientItemId else None)


def _resolveSeriesValueToFileId(value, paramTag, user, folder, perSlice):
    """Resolve a series volume ref to ``(fileId, transientItemId)``, staging per
    CLI input type.

    Dispatch on the param's declared input type (decisions.md D10): ``<image>``/
    ``<file>`` assembles the whole volume (b1, live); ``<directory>`` is the
    deferred b2 seam and raises. ``transientItemId`` is ``None`` unless a volume
    was assembled (the caller tracks it for cleanup).
    """
    if paramTag == "directory":
        # b2 (decisions.md D10): stage the file set into a transient folder and
        # bind a <directory> param. Deferred until a CLI declares <directory>.
        raise RestException(
            "Directory-input volume staging (b2) is not implemented yet", code=501
        )
    files = resolveSeriesSourceRefToFiles(value, user)
    if perSlice:
        # Escape hatch: a 2D/per-slice task binds one slice, not the volume.
        return str(files[0]["_id"]), None
    return _stageAssembledVolume(files, user, folder)


def _markJobTransients(job_doc, transientItemIds):
    """Record transient input items on the job so cleanup can delete them."""
    from girder_jobs.models.job import Job as JobModel
    try:
        JobModel().updateJob(
            job_doc, otherFields={"volviewTransient": list(transientItemIds)}
        )
    except Exception:
        logger.exception(
            "Failed to mark transient inputs on job %s", job_doc.get("_id")
        )


def _removeTransientItems(itemIds):
    """Delete transient assembled-volume items by id (idempotent, best-effort)."""
    for itemId in itemIds:
        try:
            item = Item().load(itemId, force=True)
            if item:
                Item().remove(item)
        except Exception:
            logger.exception("Failed to remove transient volume item %s", itemId)


def _cleanupTransientOnJobDone(event):
    """Delete a job's transient assembled inputs once it reaches a terminal state.

    Bound to ``jobs.job.update.after``. Idempotent: a re-fired terminal update
    finds the items already gone and no-ops.

    The job is reloaded from the database before reading the marker/status:
    ``updateJob`` fires this event with the *in-memory* job dict the updater
    passed, which only carries ``volviewTransient`` if that updater happened to
    DB-load the job first. Reloading keeps cleanup self-contained — it works for
    any terminal updater rather than relying on that external invariant.
    """
    from girder_jobs.constants import JobStatus
    from girder_jobs.models.job import Job as JobModel
    info = getattr(event, "info", None)
    eventJob = info.get("job") if isinstance(info, dict) else None
    if not isinstance(eventJob, dict):
        return
    # The marker only lives on the committed doc — the event carries the
    # updater's in-memory dict, which has it only if that updater DB-loaded the
    # job. Reload so cleanup is self-contained, then read status off the same
    # committed doc.
    job = JobModel().load(eventJob.get("_id"), force=True)
    if not isinstance(job, dict):
        return
    transientItemIds = job.get("volviewTransient")
    if not isinstance(transientItemIds, list) or not transientItemIds:
        return
    terminal = {JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED}
    if job.get("status") not in terminal:
        return
    _removeTransientItems(transientItemIds)


def _translateValuesToSlicerParams(values, doc_xml, user, folder):
    """Translate VolView values payload to slicer_cli_web's form-encoded params.

    - Series volume sourceRefs → assembled-to-one-file id (D10 b1, see above)
    - SourceRef inputs → fileId
    - ProcessingOutputRequest outputs → name + name_folder (output goes back
      to the launching folder by default)
    - Scalars → str(value)

    Returns ``(params, transientItemIds)``; the caller stamps the transient item
    ids on the job so they are cleaned up when it finishes.
    """
    inputTypes = _parseCliInputs(doc_xml or "")
    perSlice = _taskBindsSingleFile(doc_xml)
    transientItemIds = []
    params = {}
    try:
        _populateSlicerParams(
            values, inputTypes, perSlice, user, folder, params, transientItemIds
        )
    except Exception:
        # A later param failing (e.g. the deferred <directory> 501) must not
        # orphan volumes already staged this call: no job exists yet to carry
        # their ids, so the job-done cleanup would never reach them.
        _removeTransientItems(transientItemIds)
        raise
    return params, transientItemIds


def _populateSlicerParams(
    values, inputTypes, perSlice, user, folder, params, transientItemIds
):
    """Fill ``params``/``transientItemIds`` from a values payload (one param each).

    Split out so ``_translateValuesToSlicerParams`` can unwind any volumes already
    staged this call if a later param raises (the only caller).
    """
    for paramName, value in (values or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            params[paramName] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            params[paramName] = str(value)
        elif isinstance(value, str):
            # A multi-file volume ref (item 3.2) is staged for the CLI's declared
            # input type before binding; a plain file ref resolves directly.
            # Only stage when the param is a declared file input — a series ref
            # on a non-file param would otherwise assemble+upload a volume for a
            # binding the CLI cannot consume.
            paramTag = inputTypes.get(paramName)
            if paramTag is not None and decodeSeriesSourceRef(value) is not None:
                fileId, transientItemId = _resolveSeriesValueToFileId(
                    value, paramTag, user, folder, perSlice,
                )
                params[paramName] = fileId
                if transientItemId:
                    transientItemIds.append(transientItemId)
                continue
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
            # `intent` is additive: `role` stays unchanged for older clients,
            # while intent-aware clients prefer this validated field (item 3.1).
            **({"role": role} if role else {}),
            "intent": _intentForOutput(out),
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

    # Translate VolView values to slicer_cli_web params. A multi-file volume
    # input is assembled here (D10 b1) and tracked as a transient for cleanup.
    params, transientItemIds = _translateValuesToSlicerParams(
        values, cliItem.xml, user, folder
    )
    logger.info(
        "[volview_processing] runTask folder=%s task=%s params=%s",
        folder["_id"], taskId, params,
    )

    handler = genHandlerToRunDockerCLI(cliItem)
    token = Token().createToken(user=user)
    # Take a copy so the handler can mutate freely.
    job_obj = handler.subHandler(cliItem, copy.deepcopy(params), user, token)
    job_doc = job_obj.job if hasattr(job_obj, "job") else job_obj
    if transientItemIds:
        _markJobTransients(job_doc, transientItemIds)
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
    # Delete transient assembled-volume inputs (D10 b1, item 3.3) when their job
    # finishes. Bound once at plugin load.
    events.bind(
        "jobs.job.update.after",
        "girder_volview.processing",
        _cleanupTransientOnJobDone,
    )
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
