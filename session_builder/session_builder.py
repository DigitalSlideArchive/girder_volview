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
    from session_builder import create_sparse_manifest, add_rectangle, create_session_zip

API key docs: https://girder.readthedocs.io/en/latest/user-guide.html#api-keys
"""

import io
import json
import zipfile
from typing import TypedDict

MANIFEST_VERSION = "6.1.0"


class Label(TypedDict, total=False):
    name: str
    color: str
    strokeWidth: int


def create_sparse_manifest(
    data_sources: list[dict], dataset_id: str = "volume"
) -> dict:
    """
    Create minimal VolView manifest from data source URLs.

    Groups all sources into a collection and creates a named dataset.
    This ensures annotations can reliably reference the resulting volume
    regardless of which individual files successfully load.

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


def _ensure_label(
    manifest: dict,
    tool_type: str,
    label: Label | str,
) -> str:
    """Create/update label in manifest if not exists. Returns label name."""
    if isinstance(label, str):
        label_name = label
        label_config = {
            "labelName": label_name,
            "color": "red",
            "strokeWidth": 1,
            "fillColor": "transparent",
        }
    else:
        label_name = label.get("name", "default")
        label_config = {
            "labelName": label_name,
            "color": label.get("color", "red"),
            "strokeWidth": label.get("strokeWidth", 1),
            "fillColor": "transparent",
        }

    if tool_type not in manifest["tools"]:
        manifest["tools"][tool_type] = {"tools": [], "labels": {}}

    if label_name not in manifest["tools"][tool_type]["labels"]:
        manifest["tools"][tool_type]["labels"][label_name] = label_config

    return label_name


def add_rectangle(
    manifest: dict,
    image_id: str,
    first_point: list[float],
    second_point: list[float],
    slice_num: int = 0,
    plane_normal: list[float] | None = None,
    plane_origin: list[float] | None = None,
    label: Label | str = "default",
    metadata: dict | None = None,
) -> None:
    """Add rectangle annotation to manifest (mutates manifest)."""
    if plane_normal is None:
        plane_normal = [0, 0, 1]
    if plane_origin is None:
        plane_origin = [0, 0, 0]

    label_name = _ensure_label(manifest, "rectangles", label)
    tool_data = {"firstPoint": first_point, "secondPoint": second_point}
    entry = _build_tool_entry(
        tool_data, image_id, slice_num, plane_normal, plane_origin, label_name, metadata
    )
    manifest["tools"]["rectangles"]["tools"].append(entry)


def add_ruler(
    manifest: dict,
    image_id: str,
    first_point: list[float],
    second_point: list[float],
    slice_num: int = 0,
    plane_normal: list[float] | None = None,
    plane_origin: list[float] | None = None,
    label: Label | str = "default",
    metadata: dict | None = None,
) -> None:
    """Add ruler annotation to manifest (mutates manifest)."""
    if plane_normal is None:
        plane_normal = [0, 0, 1]
    if plane_origin is None:
        plane_origin = [0, 0, 0]

    label_name = _ensure_label(manifest, "rulers", label)
    tool_data = {"firstPoint": first_point, "secondPoint": second_point}
    entry = _build_tool_entry(
        tool_data, image_id, slice_num, plane_normal, plane_origin, label_name, metadata
    )
    manifest["tools"]["rulers"]["tools"].append(entry)


def add_polygon(
    manifest: dict,
    image_id: str,
    points: list[list[float]],
    slice_num: int = 0,
    plane_normal: list[float] | None = None,
    plane_origin: list[float] | None = None,
    label: Label | str = "default",
    metadata: dict | None = None,
) -> None:
    """Add polygon annotation to manifest (mutates manifest)."""
    if plane_normal is None:
        plane_normal = [0, 0, 1]
    if plane_origin is None:
        plane_origin = [0, 0, 0]

    label_name = _ensure_label(manifest, "polygons", label)
    tool_data = {"points": points}
    entry = _build_tool_entry(
        tool_data, image_id, slice_num, plane_normal, plane_origin, label_name, metadata
    )
    manifest["tools"]["polygons"]["tools"].append(entry)


def create_session_zip(manifest: dict) -> bytes:
    """Package manifest into session.volview.zip bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
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
        if not f["name"].endswith(".volview.zip")
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
                if not f["name"].endswith(".volview.zip"):
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
    image_id = annotation.get("imageId", "0")
    slice_num = annotation.get("slice", 0)
    plane_normal = annotation.get("planeNormal", [0, 0, 1])
    plane_origin = annotation.get("planeOrigin", [0, 0, 0])
    metadata = annotation.get("metadata")

    label_name = annotation.get("label", "default")
    color = annotation.get("color", "#ff0000")
    label: Label | str = {"name": label_name, "color": color, "strokeWidth": 2}

    if ann_type == "rectangle":
        add_rectangle(
            manifest,
            image_id,
            annotation["firstPoint"],
            annotation["secondPoint"],
            slice_num,
            plane_normal,
            plane_origin,
            label,
            metadata,
        )
    elif ann_type == "ruler":
        add_ruler(
            manifest,
            image_id,
            annotation["firstPoint"],
            annotation["secondPoint"],
            slice_num,
            plane_normal,
            plane_origin,
            label,
            metadata,
        )
    elif ann_type == "polygon":
        add_polygon(
            manifest,
            image_id,
            annotation["points"],
            slice_num,
            plane_normal,
            plane_origin,
            label,
            metadata,
        )


def generate_session(
    gc,
    parent_id: str,
    parent_type: str,
    annotations: list[dict] | None = None,
    upload: bool = True,
) -> tuple[dict, bytes]:
    """
    Generate session.volview.zip with optional annotations.

    Args:
        gc: Authenticated GirderClient
        parent_id: Item or folder ID
        parent_type: "item" or "folder"
        annotations: List of annotation dicts (see format below)
        upload: Whether to upload to Girder

    Returns:
        (manifest, zip_bytes)

    Annotation format:
        {
            "type": "rectangle" | "ruler" | "polygon",
            "imageId": "0",
            "firstPoint": [x, y, z],      # rectangle/ruler
            "secondPoint": [x, y, z],     # rectangle/ruler
            "points": [[x,y,z], ...],     # polygon
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

    # Annotations without imageId default to the dataset
    for annotation in annotations or []:
        if "imageId" not in annotation:
            annotation = {**annotation, "imageId": dataset_id}
        _apply_annotation(manifest, annotation)

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
