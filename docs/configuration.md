# Client Configuration file

This covers `.volview_config.yaml`, which configures the VolView client itself.
To group images and add metadata columns in Girder's file browser, see
[Customize file browsing](./customize_file_browsing.md), which uses a
separate `.large_image_config.yaml` file.

Using the client YAML file, anyone can change:

- The default view layout
- Associate files to layer or apply as segmentations via file name
- Default window and level
- The segments the painting and vector annotation tools offer

Add a `.volview_config.yaml` file higher in the folder hierarchy. Example file:

```yml
layouts:
  Axial:
    gridSize: ["axial"]
segments:
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

## Layout Configuration

Define one or more named layouts using the `layouts` key.
VolView will use the first layout as the default.
Each named layout will appear in the layout selector menu.

### Grid with Specific View Types

Use a 2D array of view type strings to specify both the grid layout and which views appear in each position:

```yml
layouts:
  Four Slice Views:
    - [axial, coronal]
    - [sagittal, axial]
```

Available view types: `axial`, `coronal`, `sagittal`, `volume`, `oblique`

### Nested Hierarchical Layout

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

### Multiple Layouts Example

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

### Simple Grid (gridSize)

Alternatively, use `gridSize` to set the layout grid as `[width, height]`:

```yml
layouts:
  Two by Two:
    gridSize: [2, 2]
```

### Disabled View Types

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

## Segment Configuration

Painting, polygons, rulers and rectangles share one registry of segments, keyed
by name under `segments`. Every appearance field is optional, so an entry states
only what it changes:

```yml
segments:
  artifact: # segment name
    color: "gray"
    strokeWidth: 3
  needs-review:
    color: "#FFBF00"
  lesion:
    color: "#ff0000"
    fillOpacity: 0.5
    outlineOpacity: 0.8
```

Fields: `color`, `fillOpacity`, `outlineOpacity`, `strokeWidth`.

Omitting `segments` leaves the registry as it is. An empty value clears what an
earlier config contributed, while keeping any segment the data still references
with its last configured appearance.

```yml
segments:
```

VolView also accepts the earlier per-tool form, `labels` with its
`defaultLabels`, `rulerLabels`, `rectangleLabels` and `polygonLabels` records.
It converts them into `segments` and warns the config's author. All four
describe the one registry, so a name appearing in more than one becomes a single
segment whose appearance comes from the first record to declare it, reading
`rulerLabels`, `rectangleLabels`, `polygonLabels` and then `defaultLabels`. A
`fillColor` is dropped, since fill is a property of the rectangle rather than of
the segment. A config carrying both keys is read from `segments` alone and its
`labels` is ignored.

## Keyboard Shortcuts Configuration

Configure the keys to activate tools, change the selected segment, and more.
Names for shortcut actions are in [constants.ts](https://github.com/Kitware/VolView/blob/main/src/constants.ts#L53) are under the `ACTIONS` variable.

To configure a key for an action, add its action name and the key(s) under the `shortcuts` section. For key combinations, use `+` like `Ctrl+f`.

```yml
shortcuts:
  polygon: "Ctrl+p"
  rectangle: "b"
```

In VolView, show a dialog with the configured keyboard shortcuts by pressing the `?` key.

## Saved Mask File Format

Edited segmentations are saved as separate mask files within session.volview.zip files. By default the mask file format is `nii.gz`.

```yml
io:
  segmentationSaveFormat: "nii.gz" # default is nii.gz
```

## Automatic Layers and Segmentations by File Name

When loading multiple image files, VolView can automatically associate related images based on file naming patterns.
For non-DICOM base images, the matching rule is based on the base filename prefix.
The extension must appear anywhere in the filename after splitting by dots,
and the filename must start with the same prefix as the base image (everything before the first dot).

For example, with a base image `patient.nrrd`:

- Layers: `patient.layer.1.pet.nii`, `patient.layer.2.ct.mha`
- Segmentations: `patient.seg.1.tumor.nii.gz`, `patient.seg.2.lesion.mha`

When multiple layers or segmentations match a base image, they are sorted alphabetically by filename and added in that order.
An image has one segmentation, so every matched file contributes its segments to that one.

### Segmentations

Use `segmentationExtension` to automatically convert matching non-DICOM images to segmentations.
For example, `myFile.seg.nrrd` becomes a segmentation for `myFile.nii`. Defaults to `"seg"`. To disable set to `""`.

```yml
io:
  segmentationExtension: "seg" # "seg" is the default
```

VolView also accepts the earlier names for these two keys, `segmentGroupExtension` and
`segmentGroupSaveFormat`. The plugin renames them to the current names as it reads a
folder's `.volview_config.yaml`, so a config written with either name still overrides the
defaults above. An `io` block that names one setting both ways is passed through as
written. The client accepts equal values but rejects the config when the values
differ. Use only the current names to avoid conflicts.

### Layering

Use `layerExtension` to automatically layer matching non-DICOM images on top of the base image.
For example, `myImage.layer.nii` is layered on top of `myImage.nii`. Defaults to `"layer"` .To disable set to `""`.

```yml
io:
  layerExtension: "layer" # "layer" is the default
```

For DICOM-specific association rules, explicit session manifests, and notes on
using DICOM tags versus file names, see
[Loading Layers and Segmentations](./loading_layers_and_segmentations.md).

## Default Window Level

Will force the window level for all loaded volumes.

```yml
windowing:
  level: 100
  width: 50
```
