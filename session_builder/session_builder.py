# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "girder-client",
# ]
# ///
"""
VolView Session Builder - Generate session.volview.zip files from Girder resources.

Usage:
    # Standalone with uv
    uv run session_builder.py --api-url URL --api-key KEY --item-id ID [--annotations file.json] [--upload]

    # As library
    from session_builder import generate_session, create_sparse_manifest, create_session_zip

API key docs: https://girder.readthedocs.io/en/latest/user-guide.html#api-keys

VolView manifest schema (Zod):
    https://github.com/Kitware/VolView/blob/main/src/io/state-file/schema.ts

Manifest migrations:
    https://github.com/Kitware/VolView/blob/main/src/io/state-file/migrations.ts
"""

import io
import json
import uuid
import zipfile
from typing import TypedDict

# Must match a version VolView can migrate to current. See migrations.ts link above.
MANIFEST_VERSION = "6.1.1"


class Label(TypedDict, total=False):
    name: str
    color: str
    strokeWidth: int
    fillColor: str


class SegmentMask(TypedDict):
    value: int
    name: str
    color: list[int]  # [R, G, B, A]
    visible: bool


class LabelMapMetadata(TypedDict):
    name: str
    parentImage: str
    segments: dict  # {"order": [...], "byValue": {...}}


class LabelMapEntry(TypedDict):
    id: str
    path: str
    metadata: LabelMapMetadata
    data: bytes  # VTI file contents


# Categorical colors for segments (VolView-like palette)
SEGMENT_COLORS = [
    [255, 0, 0, 255],      # red
    [0, 255, 0, 255],      # green
    [0, 0, 255, 255],      # blue
    [255, 255, 0, 255],    # yellow
    [255, 0, 255, 255],    # magenta
    [0, 255, 255, 255],    # cyan
    [255, 128, 0, 255],    # orange
    [128, 0, 255, 255],    # purple
    [0, 255, 128, 255],    # spring green
    [255, 128, 128, 255],  # light red
    [128, 255, 128, 255],  # light green
    [128, 128, 255, 255],  # light blue
]


def create_labelmap_entry(
    labelmap_data: bytes,
    label_names: dict[int, str],
    parent_image_id: str = "volume",
    name: str = "Segmentation",
    file_format: str = "vti",
    filename: str | None = None,
) -> LabelMapEntry:
    """
    Create a labelmap entry for inclusion in session zip.

    Args:
        labelmap_data: Binary contents of labelmap file (VTI format)
        label_names: Dict mapping segment value to name, e.g. {1: "liver", 2: "spleen"}
        parent_image_id: ID of the parent image dataset
        name: Display name for the segment group
        file_format: File extension (vti, nii.gz, etc.)
        filename: Base filename without extension (default: UUID)

    Returns:
        LabelMapEntry ready for create_session_zip
    """
    lm_id = str(uuid.uuid4())
    file_basename = filename or lm_id
    path = f"labels/{file_basename}.{file_format}"

    # Build segments metadata
    order = sorted(label_names.keys())
    by_value = {}
    for i, value in enumerate(order):
        segment_name = label_names[value]
        color = SEGMENT_COLORS[i % len(SEGMENT_COLORS)]
        by_value[str(value)] = SegmentMask(
            value=value,
            name=segment_name,
            color=color,
            visible=True,
        )

    metadata = LabelMapMetadata(
        name=name,
        parentImage=parent_image_id,
        segments={"order": order, "byValue": by_value},
    )

    return LabelMapEntry(
        id=lm_id,
        path=path,
        metadata=metadata,
        data=labelmap_data,
    )


def create_sparse_manifest(
    data_sources: list[dict], dataset_id: str = "volume"
) -> dict:
    """
    Create minimal VolView manifest from data source URLs.

    How the collection pattern works:
        1. Each URL becomes a numbered "uri" data source (id: 0, 1, 2, ...)
        2. A "collection" source groups all uri sources together
        3. A "dataset" references the collection with a stable ID
        4. When VolView loads, the collection becomes a single volume
        5. Annotations reference the dataset ID (e.g., "volume") not source IDs

    This pattern ensures annotations work regardless of how many files
    are loaded or which individual files succeed.

    Args:
        data_sources: List of {url: str, name?: str} dicts
        dataset_id: ID to assign to the dataset (used by annotations as imageID)

    Returns:
        Manifest dict with version, dataSources, datasets, and tools.
    """
    sources = []
    source_ids = []
    for i, source in enumerate(data_sources):
        entry = {"id": i, "type": "uri", "uri": source["url"]}
        if "name" in source:
            entry["name"] = source["name"]
        sources.append(entry)
        source_ids.append(i)

    # Add collection that groups all sources
    collection_id = len(sources)
    sources.append({"id": collection_id, "type": "collection", "sources": source_ids})

    # Create dataset referencing the collection
    datasets = [{"id": dataset_id, "dataSourceId": collection_id}]

    return {
        "version": MANIFEST_VERSION,
        "dataSources": sources,
        "datasets": datasets,
        "tools": {},
    }


def _build_tool_entry(
    tool_data: dict,
    image_id: str,
    slice_num: int,
    plane_normal: list[float],
    plane_origin: list[float],
    label_name: str,
    metadata: dict | None,
) -> dict:
    """Build the tool dict structure."""
    entry = {
        "imageID": image_id,
        "frameOfReference": {
            "planeOrigin": plane_origin,
            "planeNormal": plane_normal,
        },
        "slice": slice_num,
        "label": label_name,
        **tool_data,
    }
    if metadata:
        entry["metadata"] = metadata
    return entry


def _next_label_name(manifest: dict, tool_type: str) -> str:
    """Generate next label name like 'Label 1', 'Label 2', etc."""
    existing = manifest.get("tools", {}).get(tool_type, {}).get("labels", {})
    return f"Label {len(existing) + 1}"


def _ensure_label(
    manifest: dict,
    tool_type: str,
    label: Label | str | None = None,
) -> str:
    """Create/update label in manifest if not exists. Returns label name."""
    if tool_type not in manifest["tools"]:
        manifest["tools"][tool_type] = {"tools": [], "labels": {}}

    if label is None:
        label_name = _next_label_name(manifest, tool_type)
        label_config = {"labelName": label_name}
    elif isinstance(label, str):
        label_name = label
        label_config = {"labelName": label_name}
    else:
        label_name = label.get("name") or _next_label_name(manifest, tool_type)
        label_config = {"labelName": label_name, **{k: v for k, v in label.items() if k != "name"}}

    if label_name not in manifest["tools"][tool_type]["labels"]:
        manifest["tools"][tool_type]["labels"][label_name] = label_config

    return label_name


def create_session_zip(manifest: dict) -> bytes:
    """Package manifest into session.volview.zip bytes.

    If manifest contains labelMaps with 'data' fields, those are written
    as separate files in the zip and the 'data' field is removed from
    the manifest.json output.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Extract and write labelmap binary data
        if "labelMaps" in manifest:
            for lm in manifest["labelMaps"]:
                if "data" in lm:
                    zf.writestr(lm["path"], lm["data"])
                    del lm["data"]

        manifest_json = json.dumps(manifest, indent=2)
        zf.writestr("manifest.json", manifest_json)
    return buffer.getvalue()


def load_manifest_from_zip(zip_bytes: bytes) -> dict:
    """Extract and parse manifest.json from zip bytes."""
    buffer = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(buffer, "r") as zf:
        manifest_json = zf.read("manifest.json")
        return json.loads(manifest_json)


#  Girder Client Functions


def make_file_download_url(api_url: str, file_id: str, file_name: str) -> str:
    """Build proxiable file download URL."""
    api_url = api_url.rstrip("/")
    return f"{api_url}/file/{file_id}/proxiable/{file_name}"


def get_item_files(gc, item_id: str) -> list[dict]:
    """
    Get loadable files from a Girder item.

    Args:
        gc: Authenticated GirderClient
        item_id: Item ID

    Returns:
        List of {url: str, name: str, file_id: str} dicts
    """
    files = list(gc.listFile(item_id))
    api_url = gc.urlBase
    return [
        {
            "url": make_file_download_url(api_url, str(f["_id"]), f["name"]),
            "name": f["name"],
            "file_id": str(f["_id"]),
        }
        for f in files
        if not f["name"].endswith((".volview.zip", ".volview.json"))
    ]


def get_folder_files(
    gc,
    folder_id: str,
    item_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
) -> list[dict]:
    """
    Get loadable files from folder or selection set.

    Args:
        gc: Authenticated GirderClient
        folder_id: Parent folder ID
        item_ids: Optional list of item IDs to include
        folder_ids: Optional list of folder IDs to include

    Returns:
        List of {url: str, name: str, file_id: str} dicts
    """
    api_url = gc.urlBase
    result = []

    if item_ids or folder_ids:
        for item_id in item_ids or []:
            result.extend(get_item_files(gc, item_id))
        for fid in folder_ids or []:
            for item in gc.listItem(fid):
                result.extend(get_item_files(gc, str(item["_id"])))
    else:
        for item in gc.listItem(folder_id):
            files = list(gc.listFile(str(item["_id"])))
            for f in files:
                if not f["name"].endswith((".volview.zip", ".volview.json")):
                    result.append(
                        {
                            "url": make_file_download_url(
                                api_url, str(f["_id"]), f["name"]
                            ),
                            "name": f["name"],
                            "file_id": str(f["_id"]),
                        }
                    )

    return result


def upload_session(
    gc,
    parent_id: str,
    parent_type: str,
    zip_bytes: bytes,
) -> dict:
    """
    Upload session.volview.zip to Girder, returns file doc.

    Args:
        gc: Authenticated GirderClient
        parent_id: Item or folder ID
        parent_type: "item" or "folder"
        zip_bytes: The session zip bytes

    Returns:
        File document from Girder
    """
    return gc.uploadFile(
        parentId=parent_id,
        stream=io.BytesIO(zip_bytes),
        name="session.volview.zip",
        size=len(zip_bytes),
        parentType=parent_type,
        mimeType="application/zip",
    )


#  High-Level Workflow Function


def _apply_annotation(manifest: dict, annotation: dict) -> None:
    """Apply a single annotation to the manifest."""
    ann_type = annotation.get("type")
    if ann_type not in ("rectangle", "ruler", "polygon"):
        return

    tool_type = f"{ann_type}s"  # rectangles, rulers, polygons
    image_id = annotation.get("imageID", "volume")
    slice_num = annotation.get("slice", 0)
    plane_normal = annotation.get("planeNormal", [0, 0, 1])
    plane_origin = annotation.get("planeOrigin", [0, 0, 0])
    metadata = annotation.get("metadata")

    label_name = annotation.get("label", "default")
    color = annotation.get("color", "#ff0000")
    label: Label = {"name": label_name, "color": color, "strokeWidth": 2}

    label_name = _ensure_label(manifest, tool_type, label)

    if ann_type == "polygon":
        tool_data = {"points": annotation["points"]}
    else:
        tool_data = {
            "firstPoint": annotation["firstPoint"],
            "secondPoint": annotation["secondPoint"],
        }

    entry = _build_tool_entry(
        tool_data, image_id, slice_num, plane_normal, plane_origin, label_name, metadata
    )
    manifest["tools"][tool_type]["tools"].append(entry)


def generate_session(
    gc,
    parent_id: str,
    parent_type: str,
    annotations: list[dict] | None = None,
    labelmaps: list[LabelMapEntry] | None = None,
    upload: bool = True,
) -> tuple[dict, bytes]:
    """
    Generate session.volview.zip with optional annotations and labelmaps.

    Args:
        gc: Authenticated GirderClient
        parent_id: Item or folder ID
        parent_type: "item" or "folder"
        annotations: List of annotation dicts (see format below)
        labelmaps: List of LabelMapEntry dicts (from create_labelmap_entry)
        upload: Whether to upload to Girder

    Returns:
        (manifest, zip_bytes)

    Annotation format:
        {
            "type": "rectangle" | "ruler" | "polygon",
            "imageID": "volume",           # dataset ID (default: "volume")
            "firstPoint": [x, y, z],       # rectangle/ruler
            "secondPoint": [x, y, z],      # rectangle/ruler
            "points": [[x,y,z], ...],      # polygon
            "slice": 0,
            "planeNormal": [0, 0, 1],
            "planeOrigin": [0, 0, 0],
            "label": "default",
            "color": "#ff0000",
            "metadata": {"key": "value"}
        }
    """
    if parent_type == "item":
        data_sources = get_item_files(gc, parent_id)
    else:
        data_sources = get_folder_files(gc, parent_id)

    dataset_id = "volume"
    manifest = create_sparse_manifest(data_sources, dataset_id)

    for annotation in annotations or []:
        if "imageID" not in annotation:
            annotation = {**annotation, "imageID": dataset_id}
        _apply_annotation(manifest, annotation)

    if labelmaps:
        manifest["labelMaps"] = list(labelmaps)

    zip_bytes = create_session_zip(manifest)

    if upload:
        upload_session(gc, parent_id, parent_type, zip_bytes)

    return manifest, zip_bytes


# CLI Interface


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate session.volview.zip from Girder resources"
    )
    parser.add_argument("--api-url", required=True, help="Girder API URL")
    parser.add_argument("--api-key", required=True, help="Girder API key")
    parser.add_argument("--item-id", help="Item ID to generate session from")
    parser.add_argument("--folder-id", help="Folder ID to generate session from")
    parser.add_argument(
        "--annotations", help="JSON file with annotations", type=str, default=None
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload session to Girder",
    )
    parser.add_argument(
        "--output",
        help="Output file path (if not uploading)",
        type=str,
        default=None,
    )

    args = parser.parse_args()

    if not args.item_id and not args.folder_id:
        parser.error("Either --item-id or --folder-id is required")

    if args.item_id and args.folder_id:
        parser.error("Cannot specify both --item-id and --folder-id")

    from girder_client import GirderClient

    gc = GirderClient(apiUrl=args.api_url)
    gc.authenticate(apiKey=args.api_key)

    annotations = None
    if args.annotations:
        with open(args.annotations) as f:
            annotations = json.load(f)

    parent_id = args.item_id or args.folder_id
    parent_type = "item" if args.item_id else "folder"

    manifest, zip_bytes = generate_session(
        gc, parent_id, parent_type, annotations, upload=args.upload
    )

    if args.output:
        with open(args.output, "wb") as f:
            f.write(zip_bytes)
        print(f"Session saved to {args.output}")
    elif not args.upload:
        print(json.dumps(manifest, indent=2))
    else:
        print("Session uploaded to Girder")


if __name__ == "__main__":
    main()
