# VolView Session Builder

Generate VolView sessions with annotations using the Python API.

## Prerequisites

- **Girder API URL**: Your Girder server URL with `/api/v1` appended (e.g., `http://localhost:8080/api/v1`)
- **Girder API Key**: Create from your Girder user account settings ([docs](https://girder.readthedocs.io/en/latest/user-guide.html#api-keys))

## Examples

| Example                       | Description                                                                |
| ----------------------------- | -------------------------------------------------------------------------- |
| `item_session_example.py`     | Create session from a single Girder item with rectangle annotation         |
| `folder_session_example.py`   | Create session from a Girder folder with rectangle annotation              |
| `composable_example.py`       | Build session step-by-step using the composable API                        |
| `itk_analysis_example.py`     | Download image, run ITK body contour extraction, create polygon annotation |
| `totalsegmentator_example.py` | Run TotalSegmentator on CT, upload segment group with named segments       |

Run examples with `uv`:

```bash
uv run item_session_example.py --api-url URL --api-key KEY --item-id ID
uv run folder_session_example.py --api-url URL --api-key KEY --folder-id ID
uv run composable_example.py --api-url URL --api-key KEY --item-id ID
uv run itk_analysis_example.py --api-url URL --api-key KEY --item-id ID
uv run totalsegmentator_example.py --api-url URL --api-key KEY --item-id ID --fast
```

## API Reference

### `generate_session(gc, parent_id, parent_type, annotations=None, segment_groups=None, upload=True)`

Main entry point. Generates a VolView session from a Girder item or folder.

```python
from session_builder import generate_session

manifest, json_bytes = generate_session(
    gc,
    parent_id="item_or_folder_id",
    parent_type="item",  # or "folder"
    annotations=[...],       # optional annotation dicts
    segment_groups=[...],    # optional SegmentGroupInput dicts
    upload=True,
)
```

### Composable API

Build manifests incrementally with these functions (each returns a new copy):

```python
from session_builder import (
    create_manifest, add_dataset, add_annotation, add_segment_group,
    serialize_manifest, upload_session, get_folder_files
)

# Build manifest step by step
manifest = create_manifest()
manifest = add_dataset(manifest, get_folder_files(gc, folder_id), "volume")
manifest = add_annotation(manifest, {"type": "rectangle", ...}, dataset_id="volume")
manifest = add_segment_group(manifest, url="...", dataset_id="volume", label_names={1: "liver"})

# Serialize and upload
json_bytes = serialize_manifest(manifest)
upload_session(gc, folder_id, "folder", json_bytes)
```

#### `create_manifest()`

Create an empty VolView manifest with version, empty dataSources/datasets, and tools.

#### `add_dataset(manifest, data_sources, dataset_id)`

Add a dataset to the manifest. If multiple data sources, creates a collection to group them.

#### `add_annotation(manifest, annotation, dataset_id=None)`

Add an annotation to the manifest. `dataset_id` specifies the target dataset (falls back to `annotation["imageID"]`, then `"volume"`). See [Annotation Format](#annotation-format) below.

#### `add_segment_group(manifest, url, dataset_id, label_names, name="Segmentation")`

Add a segment group with named segments. `dataset_id` is the parent image dataset. `label_names` maps segment values to names (e.g., `{1: "liver", 2: "spleen"}`).

### `upload_segment_group(gc, segment_group_bytes, filename, parent_id, parent_type)`

Upload a segment group file to Girder, returns download URL for use in `SegmentGroupInput`.

### `get_item_files(gc, item_id)` / `get_folder_files(gc, folder_id)`

Get loadable file URLs from Girder item or folder. Returns `[{url, name, file_id}, ...]`.

### `download_folder_files(gc, folder_id, dest_dir)`

Download all files from a folder to local directory. Returns list of file paths.

## How It Works

The session builder creates a `session.volview.json` that configures VolView.

### Multi-file volumes (DICOM series)

For DICOM or other multi-file volumes, a collection groups files into a single dataset:

1. Each file URL becomes a numbered "uri" data source (id: 0, 1, 2, ...)
2. A "collection" source groups all uri sources together
3. A "dataset" with ID `"volume"` references the collection
4. Annotations reference the dataset ID (`"volume"`), not individual source IDs

## Annotation Format

```python
{
    "type": "rectangle" | "ruler" | "polygon",  # required
    "firstPoint": [x, y, z],           # required for rectangle/ruler
    "secondPoint": [x, y, z],          # required for rectangle/ruler
    "points": [[x, y, z], ...],        # required for polygon
    "slice": 0,                        # required
    "planeNormal": [0, 0, 1],          # required
    "planeOrigin": [0, 0, 0],          # required
    "imageID": "volume",               # default: "volume"
    "label": "lesion",                 # default: "default"
    "color": "#ff0000",                # default: "#ff0000"
    "metadata": {"key": "value"}       # optional
}
```
