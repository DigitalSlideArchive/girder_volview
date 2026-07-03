# Girder Plugin for VolView

Open Items in [VolView](https://github.com/Kitware/VolView) with a "Open in VolView" button. The button is located in the top right, on an Item's page.

## Supported Image File Formats

- DICOM `.dcm`
- Nrrd `.nrrd`
- NIFTI `.nii`
- VTK image `.vti`
- And many more. Try dragging and dropping the file(s) on the [VolView Demo Site](https://volview.netlify.app/)

## Client Configuration file

Using the client YAML file, anyone can change:

- The default view layout
- Associate files to layer or apply as segmentations via file name
- Default window and level
- Default labels for vector annotation tools

Add a `.volview_config.yaml` file higher in the folder hierarchy. Example file:

```yml
layouts:
  Axial:
    gridSize: ["axial"]
labels:
  defaultLabels:
    artifact:
      color: "gray"
      strokeWidth: 3
    needs-review:
      color: "#FFBF00"
```

To merge with `.volview_config.yaml`s higher in the folder hierarchy, include `__inherit__: true`
in the child `.volview_config.yaml` file. Example:

Child `.volview_config.yaml`

```yml
__inherit__: true
shortcuts:
  polygon: "Ctrl+p"
  rectangle: "b"
```

Parent `.volview_config.yaml`

```yml
layouts:
  Axial:
    gridSize: ["axial"]
```

Result

```yml
shortcuts:
  polygon: "Ctrl+p"
  rectangle: "b"
layouts:
  Axial:
    gridSize: ["axial"]
```

### Layout Configuration

Define one or more named layouts using the `layouts` key.
VolView will use the first layout as the default.
Each named layout will appear in the layout selector menu.

#### Grid with Specific View Types

Use a 2D array of view type strings to specify both the grid layout and which views appear in each position:

```yml
layouts:
  Four Slice Views:
    - [axial, coronal]
    - [sagittal, axial]
```

Available view types: `axial`, `coronal`, `sagittal`, `volume`, `oblique`

#### Nested Hierarchical Layout

For complex layouts, use this nested structure:

```yml
layouts:
  Axial Primary:
    direction: row
    items:
      - axial
      - direction: column
        items:
          - coronal
          - sagittal
```

Direction values:

- `row` - items arranged horizontally
- `column` - items stacked vertically

View object properties:

- 2D views: `type: 2D`, `orientation: Axial|Coronal|Sagittal`, `name` (optional)
- 3D views: `type: 3D`, `viewDirection` (optional), `viewUp` (optional), `name` (optional)
- Oblique views: `type: Oblique`, `name` (optional)

#### Multiple Layouts Example

Define multiple named layouts that users can switch between:

```yml
layouts:
  Three Slice Views:
    - [axial, coronal]
    - [sagittal, axial]
  Axial Focus:
    direction: row
    items:
      - axial
      - direction: column
        items:
          - coronal
          - sagittal
```

#### Simple Grid (gridSize)

Alternatively, use `gridSize` to set the layout grid as `[width, height]`:

```yml
layouts:
  Two by Two:
    gridSize: [2, 2]
```

#### Disabled View Types

Prevent certain view types from appearing in the view type switcher with this config option. The 3D and Oblique types are disabled by default:

```yml
disabledViewTypes:
  - 3D
  - Oblique
```

To enable 3D and Oblique views, use an empty list:

```yml
disabledViewTypes: []
```

Valid values: `2D`, `3D`, `Oblique`

### Label Configuration

To assign labels and their properties, add a `.volview_config.yaml` file higher in the folder hierarchy.  
Example `.volview_config.yaml` file:

```yml
# defaultLabels are shared by polygon, ruler and rectangle tool
labels:
  defaultLabels:
    artifact:
      color: "gray"
      strokeWidth: 3
    needs-review:
      color: "#FFBF00"
```

Labels can be configured per tool:

```yml
labels:
  rectangleLabels:
    lesion: # label name
      color: "#ff0000"
      fillColor: "transparent"
    innocuous:
      color: "white"
      fillColor: "#00ff0030"
    tumor:
      color: "green"
      fillColor: "transparent"

  rulerLabels:
    big:
      color: "#ff0000"
    small:
      color: "white"
```

Label sections could be empty to disable labels for a tool.

```yml
labels:
  rulerLabels:

  rectangleLabels:
    lesion:
      color: "#ff0000"
      fillColor: "transparent"
    innocuous:
      color: "white"
      fillColor: "#00ff0030"
```

### Keyboard Shortcuts Configuration

Configure the keys to activate tools, change selected labels, and more.
Names for shortcut actions are in [constants.ts](https://github.com/Kitware/VolView/blob/main/src/constants.ts#L53) are under the `ACTIONS` variable.

To configure a key for an action, add its action name and the key(s) under the `shortcuts` section. For key combinations, use `+` like `Ctrl+f`.

```yml
shortcuts:
  polygon: "Ctrl+p"
  rectangle: "b"
```

In VolView, show a dialog with the configured keyboard shortcuts by pressing the `?` key.

### Saved Segment Group File Format

Edited segment groups are saved as separate files within session.volview.zip files.  By default the segment group file format is `nii.gz`.

```yml
io:
  segmentGroupSaveFormat: "nii.gz" # default is nii.gz
```

### Automatic Layers and Segment Groups by File Name

When loading multiple image files, VolView can automatically associate related images based on file naming patterns.
For non-DICOM base images, the matching rule is based on the base filename prefix.
The extension must appear anywhere in the filename after splitting by dots,
and the filename must start with the same prefix as the base image (everything before the first dot).

For example, with a base image `patient.nrrd`:

- Layers: `patient.layer.1.pet.nii`, `patient.layer.2.ct.mha`
- Segment groups: `patient.seg.1.tumor.nii.gz`, `patient.seg.2.lesion.mha`

When multiple layers or segment groups match a base image, they are sorted alphabetically by filename and added in that order.

#### Segment Groups

Use `segmentGroupExtension` to automatically convert matching non-DICOM images to segment groups.
For example, `myFile.seg.nrrd` becomes a segment group for `myFile.nii`. Defaults to `"seg"`. To disable set to `""`.

```yml
io:
  segmentGroupExtension: "seg" # "seg" is the default
```

#### Layering

Use `layerExtension` to automatically layer matching non-DICOM images on top of the base image.
For example, `myImage.layer.nii` is layered on top of `myImage.nii`. Defaults to `"layer"` .To disable set to `""`.

```yml
io:
  layerExtension: "layer" # "layer" is the default
```

For DICOM-specific association rules, explicit `segmentGroups` /
`parentToLayers` session manifest examples, and notes on using DICOM tags versus
file names, see [Loading Layers and Segmentations](./docs/loading_layers_and_segmentations.md).

### Default Window Level

Will force the window level for all loaded volumes.

```yml
windowing:
  level: 100
  width: 50
```

## Session Builder

Generate VolView sessions programmatically with Python. Create sessions with annotations or labelmaps from analysis pipelines.

See [session_builder/README.md](./session_builder/README.md) for API docs and examples.

## Customize File Browsing to Group Images and add Columns

A `.large_image_config.yaml` file can change how images are grouped
and display columns with image metadata.

[Example YAMLs and docs](./docs/customize_file_browsing.md)

## Speedup S3 file downloading by disabling proxying

The VolView plugin proxies request to download files from S3 by default.
This avoids a CORS error when loading a file from an S3 bucket asset store without CORS configuration.
To speed up downloading of files from S3, the Girder admin can:

1. [Configure CORS](https://girder.readthedocs.io/en/stable/user-guide.html#s3) in the S3 bucket for the Girder server.
2. Change the global [Girder configuration](https://girder.readthedocs.io/en/stable/configuration.html) to add
   a `[volview]` section with a `proxy_assetstores = False` option. See below:

```
[volview]
# Workaround CORS configuration errors in S3 assetstores.
# If True, the Girder server will proxy file download requests from
# VolView clients to the S3 assetstore. This will use more server bandwidth.
# If False, VolView client requests to download files are redirected to S3.
# Defaults to True.
proxy_assetstores = False
```

## API Endpoints

- GET folder/:id/volview?items=[itemIds]&folders=[folderIds] -> download JSON with URLS to files or the latest `*.volview.zip` file in the folder
- GET item/:id/volview -> download JSON with URLs to all files in item or the latest `*.volview.zip` file
- POST item/:id/volview -> upload file to Item with cookie authentication
- GET file/:id/proxiable/:name -> download a file with option to proxy
- GET folder/:id/volview_config/:name -> download JSON with VolView config properties
- Deprecated: GET item/:id/volview/datasets -> download all files in item except the `*.volview.zip`

## Example Saving Roundtrip flow

### Open Item

1. User clicks Open in VolView for Item - Plugin checks if `*volview.zip` file exists in Item, finds none:
   Opens VolView with file download url `item/:id/volview/datasets`
1. VolView opens, fetches from `item/:id/volview/datasets`, receives zip of all files in Item except files ending in `*volview.zip`
1. In VolView, User clicks the Save button - VolView POSTs session.volview.zip to `item/:id/volview`
1. girder_volview plugin saves new session.volview.zip in Item.
1. User clicks Open in VolView for Item - Plugin finds a `*volview.zip` in the Item. Opens VolView with file download URL pointing to `item/:id/volview`
1. VolView opens, fetches from `item/:id/volview`, receives most recently created `*volview.zip` file in Item.

VolView creates a new session.volview.zip file in the Girder Item every time the Save button is clicked.

### Open Checked

1. User checks a set of items or folders. Clicks "Open Checked in VolView".
1. Browser client updates the `lastOpened` metadata on a checked item/folder metadata with the current time.
1. Browser opens VolView with file download url pointing to `GET folder/:id/volview?items=[...ids]&folders=[...ids]`. That endpoint returns a JSON file with URLs to Girder files.
1. VolView save URL is pointing to `PUT folder/:id/volview?metadata={items: [...ids], folders: [...ids]}`. `metadata` parameter matches the checked set in the Girder file browser. User clicks save. `session.volview.zip` item is created in the folder with a `linkedResources` metadata key holding the folder and item IDs. If user checked a session.volview.zip item, then `items` points to an existing session.volview.zip. The new session.volview.zip takes the `linkedResources` of the older session.volview.zip.
1. If user clicks refresh in VolView, the `GET folder/:id/volview?items=[...ids]&folders=[...ids]` end point is hit again. If a session.volview.zip is in the `items` parameter, the plugin reads the volview.zip's `linkedResources` and searches for a newer session.volview.zips with matching `linkedResources` and returns that if found.
1. If user checks a new set of folders or items that does not include a session.volview.zip item, the `GET folder/:id/volview` endpoint does not pick a session.volview.zip with matching `linkedResources` as `lastOpened` metadata on one of the checked items/folders is newer than the matching session.volview.zip. This allows opening of images with a clean slate.

### Open Filter-Linked Session (Grouped DICOM Row)

Filter-linked sessions use `linkedResources.filter` (a metadata-key/value dict like `{"meta.dicom.StudyInstanceUID": "..."}`) in place of explicit item/folder IDs. The grouped DICOM row opener produces these.

1. User clicks Open on a grouped row. Browser opens VolView with a manifest URL of `GET folder/:id/volview?filters={...}`. The endpoint returns the newest session.volview.zip whose `linkedResources.filter` is *equal* to the row's filter (strict set-equality on the filter list); if none exists, it returns the raw DICOM files matching the filter.
1. User clicks Save. A new session.volview.zip is created in the folder with the row's filter recorded under `linkedResources.filter`.
1. User clicks refresh in VolView. The same `?filters={...}` URL is hit again and now resolves to the just-saved session (newest matching by `getTouchedTime`, which honors `meta.lastOpened`).
1. User checks an older filter-linked session item in the file browser and clicks "Open Checked in VolView". Client bumps `lastOpened` on the checked item, then opens VolView with `?items=<id>`. The endpoint reads the checked session's `linkedResources.filter` and returns the newest matching session — which is the just-touched older one. Subsequent saves create newer matching sessions; refresh picks them up via the same touched-time rule. This is what makes "go back in history" and "refresh after save" use the same code path.

## Development

Get this running https://github.com/DigitalSlideArchive/digital_slide_archive/tree/master/devops/with-dive-volview

In the `docker-compose.override.yml` file, add volumes pointing to this Girder
plugin. If you want to use a local VolView build, mount that build's `dist`
directory over the packaged VolView `dist` directory. Example:

```yaml
services:
  girder:
    volumes:
      - ../with-dive-volview/provision.divevolview.yaml:/opt/digital_slide_archive/devops/dsa/provision.yaml
      - ../../../girder_volview:/opt/girder_volview
      - ../../../../VolView/dist:/opt/girder_volview/girder_volview/web_client/node_modules/volview/dist:ro
```

Comment out the pip install of this plugin here: https://github.com/DigitalSlideArchive/digital_slide_archive/blob/master/devops/with-dive-volview/provision.divevolview.yaml#L3

To install volume mapped girder-volview plugin and incorporate changes as files are edited, add this to the `shell` section of the provision.yaml:

```yaml
shell:
  - cd /opt/girder_volview/ && pip install -e .
  - (sleep 30 && girder build --dev --watch-plugin volview)&
```

### Develop VolView client

To develop with a local VolView build, build VolView from source and mount or
copy its `dist` directory over `girder_volview/web_client/node_modules/volview/dist`
before rebuilding the Girder web client. The checked-in Webpack helper always
copies from the packaged `node_modules/volview/dist` path, so local development
does not require changing `webpack.helper.js`.

Then build VolView from source:

```sh
npm run build
```

Processing (the Analysis/Jobs tab) and remote session save ship in every build
and no longer need build-time env flags — `VITE_ENABLE_PROCESSING`,
`VITE_ENABLE_REMOTE_SAVE`, and `VITE_PROCESSING_ALLOWED_ORIGINS` were removed.
What the deployed client is allowed to contact is decided at runtime by the
origin allow-list; a same-origin deployment (such as DSA) needs no configuration.
See [Processing provider & remote-save origin allow-list](docs/processing_origin_allow_list.md).

### Updating the VolView Client Version

1. Update the [volview](https://www.npmjs.com/package/volview?activeTab=versions) version in `./girder_volview/web_client/package.json`
