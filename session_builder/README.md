# VolView Session Builder

Generate VolView sessions with annotations using the Python API.

## Prerequisites

### Girder API URL

The API URL is your Girder server URL with `/api/v1` appended.

Example: `http://localhost:8080/api/v1`

### Girder API Key

Create an API key from your Girder user account settings.

Docs: https://girder.readthedocs.io/en/latest/user-guide.html#api-keys

### Item ID

The Girder item ID containing the image files you want to load in VolView. The generated `session.volview.zip` will be uploaded to this same item.

## Example

Uses `session_builder.py` to generate sessions programmatically.

```bash
uv run item_session_example.py \
  --api-url http://localhost:8080/api/v1 \
  --api-key YOUR_API_KEY \
  --item-id ITEM_ID
```

With values

```bash
uv run item_session_example.py \
  --api-url http://localhost:8080/api/v1 \
  --api-key hEbIt0ZEJPZPEgbKTnlCjwudx3SXOfiLrhcBSNkO \
  --item-id 689e31559a0902efec348da2
```

## How It Works

The session builder creates a `session.volview.zip` containing a `manifest.json` that configures VolView.

**Data Source → Collection → Dataset pattern:**

1. Each file URL becomes a numbered "uri" data source (id: 0, 1, 2, ...)
2. A "collection" source groups all uri sources together
3. A "dataset" with ID `"volume"` references the collection
4. When VolView loads, the collection becomes a single volume
5. Annotations reference the dataset ID (`"volume"`), not individual source IDs

This ensures annotations work regardless of how many files are loaded.

## Annotation Format

Annotations are dictionaries with the following fields:

```python
{
    "type": "rectangle" | "ruler" | "polygon",
    "imageID": "volume",               # dataset ID (defaults to "volume")
    "firstPoint": [x, y, z],           # rectangle/ruler
    "secondPoint": [x, y, z],          # rectangle/ruler
    "points": [[x, y, z], ...],        # polygon only
    "slice": 0,                        # slice number
    "planeNormal": [0, 0, 1],          # optional, default axial
    "planeOrigin": [0, 0, 0],          # optional
    "label": "lesion",                 # label name
    "color": "#ff0000",                # optional
    "metadata": {"key": "value"}       # optional
}
```
