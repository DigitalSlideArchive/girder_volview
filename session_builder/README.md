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
| `itk_analysis_example.py`     | Download image, run ITK body contour extraction, create polygon annotation |
| `totalsegmentator_example.py` | Run TotalSegmentator on CT, upload labelmap with named segments            |

Run examples with `uv`:

```bash
uv run item_session_example.py --api-url URL --api-key KEY --item-id ID
uv run folder_session_example.py --api-url URL --api-key KEY --folder-id ID
uv run itk_analysis_example.py --api-url URL --api-key KEY --item-id ID
uv run totalsegmentator_example.py --api-url URL --api-key KEY --item-id ID --fast
```

## API Reference

### `generate_session(gc, parent_id, parent_type, annotations=None, labelmaps=None, upload=True)`

Main entry point. Generates a VolView session from a Girder item or folder.

```python
from session_builder import generate_session

manifest, json_bytes = generate_session(
    gc,
    parent_id="item_or_folder_id",
    parent_type="item",  # or "folder"
    annotations=[...],   # optional annotation dicts
    labelmaps=[...],     # optional LabelMapInput dicts
    upload=True,
)
```

### `create_sparse_manifest(data_sources, dataset_id="volume")`

Create minimal VolView manifest from data source URLs.

```python
from session_builder import create_sparse_manifest

manifest = create_sparse_manifest([
    {"url": "https://example.com/image.nii.gz", "name": "CT scan"}
])
```

### `upload_labelmap(gc, labelmap_bytes, filename, parent_id, parent_type)`

Upload a labelmap file to Girder, returns download URL for use in `LabelMapInput`.

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
