# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "girder-client",
# ]
# ///
"""
VolView Session Builder - Generate session.volview.json files from Girder resources.

Usage:
    # Standalone with uv
    uv run session_builder.py --api-url URL --api-key KEY --item-id ID [--annotations file.json] [--upload]

    # As library (high-level)
    from session_builder import generate_session
    manifest, json_bytes = generate_session(gc, parent_id, "folder", annotations, segment_groups)

    # As library (composable)
    from session_builder import (
        create_manifest, add_dataset, add_annotation, add_segment_group,
        serialize_manifest, upload_session, get_folder_files
    )
    manifest = create_manifest()
    manifest = add_dataset(manifest, get_folder_files(gc, folder_id), "volume")
    manifest = add_annotation(manifest, annotation)
    manifest = add_segment_group(manifest, url, dataset_id="volume", label_names=label_names)
    json_bytes = serialize_manifest(manifest)
    upload_session(gc, folder_id, "folder", json_bytes)

API key docs: https://girder.readthedocs.io/en/latest/user-guide.html#api-keys

VolView manifest schema (Zod):
    https://github.com/Kitware/VolView/blob/main/src/io/state-file/schema.ts
"""

import copy
import io
import json
import uuid
from pathlib import Path
from typing import TypedDict
from girder_client import GirderClient

# Must match a version VolView can migrate to current. See migrations.ts link above.
MANIFEST_VERSION = "6.2.0"

SESSION_FILE_EXTENSIONS = (".volview.zip", ".volview.json")


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


class SegmentGroupMetadata(TypedDict):
    name: str
    parentImage: str
    segments: dict  # {"order": [...], "byValue": {...}}


class SegmentGroupEntry(TypedDict):
    id: str
    dataSourceId: int
    metadata: SegmentGroupMetadata


class SegmentGroupInput(TypedDict):
    url: str  # Download URL for the segment group file
    name: str  # Display name for the segment group
    label_names: dict[int, str]  # {1: "liver", 2: "spleen", ...}


# Categorical colors for segments (VolView-like palette)
SEGMENT_COLORS = [
    [255, 0, 0, 255],  # red
    [0, 255, 0, 255],  # green
    [0, 0, 255, 255],  # blue
    [255, 255, 0, 255],  # yellow
    [255, 0, 255, 255],  # magenta
    [0, 255, 255, 255],  # cyan
    [255, 128, 0, 255],  # orange
    [128, 0, 255, 255],  # purple
    [0, 255, 128, 255],  # spring green
    [255, 128, 128, 255],  # light red
    [128, 255, 128, 255],  # light green
    [128, 128, 255, 255],  # light blue
]


def create_segment_group_entry(
    data_source_id: int,
    label_names: dict[int, str],
    parent_image_id: str = "volume",
    name: str = "Segmentation",
) -> SegmentGroupEntry:
    """
    Create a segment group entry that references a URI data source.

    Args:
        data_source_id: ID of the data source in manifest's dataSources array
        label_names: Dict mapping segment value to name, e.g. {1: "liver", 2: "spleen"}
        parent_image_id: ID of the parent image dataset
        name: Display name for the segment group

    Returns:
        SegmentGroupEntry for inclusion in manifest
    """
    sg_id = str(uuid.uuid4())

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

    metadata = SegmentGroupMetadata(
        name=name,
        parentImage=parent_image_id,
        segments={"order": order, "byValue": by_value},
    )

    return SegmentGroupEntry(
        id=sg_id,
        dataSourceId=data_source_id,
        metadata=metadata,
    )


def create_manifest() -> dict:
    """
    Create an empty VolView manifest.

    Returns:
        Manifest dict with version, empty dataSources/datasets, and tools.
    """
    return {
        "version": MANIFEST_VERSION,
        "dataSources": [],
        "datasets": [],
        "tools": {},
    }


def add_dataset(manifest: dict, data_sources: list[dict], dataset_id: str) -> dict:
    """
    Add a dataset to the manifest. Returns a new manifest copy.

    How the collection pattern works:
        1. Each URL becomes a numbered "uri" data source
        2. If multiple sources, a "collection" groups them together
        3. A "dataset" references the collection (or single source)
        4. When VolView loads, the collection becomes a single volume
        5. Annotations reference the dataset ID, not source IDs

    Args:
        manifest: Existing manifest dict
        data_sources: List of {url: str, name?: str} dicts
        dataset_id: ID to assign to the dataset (used by annotations as imageID)

    Returns:
        New manifest dict with the dataset added.
    """
    new_sources = list(manifest["dataSources"])
    start_id = len(new_sources)

    source_ids = []
    for i, source in enumerate(data_sources):
        entry = {"id": start_id + i, "type": "uri", "uri": source["url"]}
        if "name" in source:
            entry["name"] = source["name"]
        new_sources.append(entry)
        source_ids.append(start_id + i)

    if len(source_ids) > 1:
        collection_id = len(new_sources)
        new_sources.append(
            {"id": collection_id, "type": "collection", "sources": source_ids}
        )
        data_source_id = collection_id
    else:
        data_source_id = source_ids[0] if source_ids else start_id

    new_datasets = list(manifest["datasets"])
    new_datasets.append({"id": dataset_id, "dataSourceId": data_source_id})

    new_manifest = {
        **manifest,
        "dataSources": new_sources,
        "datasets": new_datasets,
    }

    if "primarySelection" not in manifest:
        new_manifest["primarySelection"] = dataset_id

    return new_manifest


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


def add_annotation(
    manifest: dict, annotation: dict, dataset_id: str | None = None
) -> dict:
    """
    Add an annotation to the manifest. Returns a new manifest copy.

    Args:
        manifest: Existing manifest dict
        annotation: Annotation dict with format:
            {
                "type": "rectangle" | "ruler" | "polygon",
                "imageID": "volume",           # dataset ID (fallback if dataset_id not provided)
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
        dataset_id: Target dataset ID. Falls back to annotation["imageID"], then "volume".

    Returns:
        New manifest dict with the annotation added.
    """
    ann_type = annotation.get("type")
    if ann_type not in ("rectangle", "ruler", "polygon"):
        return manifest

    new_tools = copy.deepcopy(manifest.get("tools", {}))

    tool_type = f"{ann_type}s"
    image_id = dataset_id or annotation.get("imageID", "volume")
    slice_num = annotation.get("slice", 0)
    plane_normal = annotation.get("planeNormal", [0, 0, 1])
    plane_origin = annotation.get("planeOrigin", [0, 0, 0])
    metadata = annotation.get("metadata")

    label_name = annotation.get("label", "default")
    color = annotation.get("color", "#ff0000")
    label: Label = {"name": label_name, "color": color, "strokeWidth": 2}

    if tool_type not in new_tools:
        new_tools[tool_type] = {"tools": [], "labels": {}}

    if label_name not in new_tools[tool_type]["labels"]:
        new_tools[tool_type]["labels"][label_name] = {
            "labelName": label_name,
            **{k: v for k, v in label.items() if k != "name"},
        }

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
    new_tools[tool_type]["tools"].append(entry)

    return {**manifest, "tools": new_tools}


def add_segment_group(
    manifest: dict,
    url: str,
    dataset_id: str,
    label_names: dict[int, str],
    name: str = "Segmentation",
) -> dict:
    """
    Add a segment group to the manifest. Returns a new manifest copy.

    Args:
        manifest: Existing manifest dict
        url: Download URL for the segment group file
        dataset_id: ID of the parent image dataset
        label_names: Dict mapping segment value to name, e.g. {1: "liver", 2: "spleen"}
        name: Display name for the segment group

    Returns:
        New manifest dict with the segment group added.
    """
    new_sources = list(manifest["dataSources"])
    data_source_id = len(new_sources)

    new_sources.append(
        {
            "id": data_source_id,
            "type": "uri",
            "uri": url,
        }
    )

    new_datasets = list(manifest["datasets"])
    new_datasets.append(
        {
            "id": str(data_source_id),
            "dataSourceId": data_source_id,
        }
    )

    segment_group_entry = create_segment_group_entry(
        data_source_id=data_source_id,
        label_names=label_names,
        parent_image_id=dataset_id,
        name=name,
    )

    new_segment_groups = list(manifest.get("segmentGroups", []))
    new_segment_groups.append(segment_group_entry)

    return {
        **manifest,
        "dataSources": new_sources,
        "datasets": new_datasets,
        "segmentGroups": new_segment_groups,
    }


def serialize_manifest(manifest: dict) -> bytes:
    """Serialize manifest to JSON bytes for session.volview.json."""
    return json.dumps(manifest, indent=2).encode("utf-8")


#  Girder Client Functions


def make_file_download_url(api_url: str, file_id: str, file_name: str) -> str:
    """Build proxiable file download URL."""
    api_url = api_url.rstrip("/")
    return f"{api_url}/file/{file_id}/proxiable/{file_name}"


def get_item_files(gc: GirderClient, item_id: str) -> list[dict]:
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
        if not f["name"].endswith(SESSION_FILE_EXTENSIONS)
    ]


def download_item_files(gc: GirderClient, item_id: str, dest_dir: Path) -> Path:
    """Download first file from item, return local path."""
    files = list(gc.listFile(item_id))
    if not files:
        raise ValueError(f"No files in item {item_id}")

    file_info = files[0]
    local_path = dest_dir / file_info["name"]
    gc.downloadFile(file_info["_id"], str(local_path))
    return local_path


def get_folder_files(
    gc: GirderClient,
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
    result = []
    items_to_process = []

    if item_ids:
        items_to_process.extend(item_ids)
    if folder_ids:
        for fid in folder_ids:
            items_to_process.extend(str(item["_id"]) for item in gc.listItem(fid))
    if not items_to_process:
        items_to_process = [str(item["_id"]) for item in gc.listItem(folder_id)]

    for item_id in items_to_process:
        result.extend(get_item_files(gc, item_id))

    return result


def upload_session(
    gc: GirderClient,
    parent_id: str,
    parent_type: str,
    json_bytes: bytes,
) -> dict:
    """
    Upload session.volview.json to Girder, returns file doc.

    Args:
        gc: Authenticated GirderClient
        parent_id: Item or folder ID
        parent_type: "item" or "folder"
        json_bytes: The session JSON bytes

    Returns:
        File document from Girder
    """
    return gc.uploadFile(
        parentId=parent_id,
        stream=io.BytesIO(json_bytes),
        name="session.volview.json",
        size=len(json_bytes),
        parentType=parent_type,
        mimeType="application/json",
    )


def upload_segment_group(
    gc: GirderClient,
    segment_group_bytes: bytes,
    filename: str,
    parent_id: str,
    parent_type: str,
) -> str:
    """
    Upload segment group file to Girder and return download URL.

    Args:
        gc: Authenticated GirderClient
        segment_group_bytes: Binary contents of segment group file
        filename: Filename for the uploaded file (e.g. "segmentation.seg.nii.gz")
        parent_id: Item or folder ID
        parent_type: "item" or "folder"

    Returns:
        Download URL for the uploaded file
    """
    file_doc = gc.uploadFile(
        parentId=parent_id,
        stream=io.BytesIO(segment_group_bytes),
        name=filename,
        size=len(segment_group_bytes),
        parentType=parent_type,
    )
    return make_file_download_url(gc.urlBase, str(file_doc["_id"]), filename)


def download_folder_files(
    gc: GirderClient,
    folder_id: str,
    dest_dir: Path,
    extra_exclude: tuple[str, ...] = (),
) -> list[Path]:
    """
    Download all files from folder items, filtering session files.

    Args:
        gc: Authenticated GirderClient
        folder_id: Folder ID to download from
        dest_dir: Directory to download files into
        extra_exclude: Additional file extensions to exclude

    Returns:
        List of downloaded file paths
    """
    files_dir = dest_dir / "files"
    files_dir.mkdir()

    exclude = SESSION_FILE_EXTENSIONS + extra_exclude
    downloaded = []
    for item in gc.listItem(folder_id):
        for file_info in gc.listFile(item["_id"]):
            if file_info["name"].endswith(exclude):
                continue
            local_path = files_dir / file_info["name"]
            gc.downloadFile(file_info["_id"], str(local_path))
            downloaded.append(local_path)
    return downloaded


def generate_session(
    gc: GirderClient,
    parent_id: str,
    parent_type: str,
    annotations: list[dict] | None = None,
    segment_groups: list[SegmentGroupInput] | None = None,
    upload: bool = True,
) -> tuple[dict, bytes]:
    """
    Generate session.volview.json with optional annotations and segment groups.

    Args:
        gc: Authenticated GirderClient
        parent_id: Item or folder ID
        parent_type: "item" or "folder"
        annotations: List of annotation dicts (see format below)
        segment_groups: List of SegmentGroupInput dicts with url, name, label_names
        upload: Whether to upload to Girder

    Returns:
        (manifest, json_bytes)

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

    SegmentGroupInput format:
        {
            "url": "https://.../seg.nii.gz",  # Download URL for segment group
            "name": "TotalSegmentator",       # Display name
            "label_names": {1: "liver", ...}  # Segment value -> name mapping
        }
    """
    if parent_type == "item":
        data_sources = get_item_files(gc, parent_id)
    else:
        data_sources = get_folder_files(gc, parent_id)

    # Filter out segment group URLs from main image sources to avoid loading them twice
    if segment_groups:
        segment_group_urls = {sg["url"] for sg in segment_groups}
        data_sources = [ds for ds in data_sources if ds["url"] not in segment_group_urls]

    dataset_id = "volume"
    manifest = create_manifest()
    manifest = add_dataset(manifest, data_sources, dataset_id)

    for annotation in annotations or []:
        manifest = add_annotation(manifest, annotation, dataset_id=dataset_id)

    for sg_input in segment_groups or []:
        manifest = add_segment_group(
            manifest,
            url=sg_input["url"],
            dataset_id=dataset_id,
            label_names=sg_input["label_names"],
            name=sg_input["name"],
        )

    json_bytes = serialize_manifest(manifest)

    if upload:
        upload_session(gc, parent_id, parent_type, json_bytes)

    return manifest, json_bytes


# CLI Interface


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate session.volview.json from Girder resources"
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

    gc = GirderClient(apiUrl=args.api_url)
    gc.authenticate(apiKey=args.api_key)

    annotations = None
    if args.annotations:
        with open(args.annotations) as f:
            annotations = json.load(f)

    parent_id = args.item_id or args.folder_id
    parent_type = "item" if args.item_id else "folder"

    manifest, json_bytes = generate_session(
        gc, parent_id, parent_type, annotations, upload=args.upload
    )

    if args.output:
        with open(args.output, "wb") as f:
            f.write(json_bytes)
        print(f"Session saved to {args.output}")
    elif not args.upload:
        print(json.dumps(manifest, indent=2))
    else:
        print("Session uploaded to Girder")


if __name__ == "__main__":
    main()
