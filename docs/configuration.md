# Client Configuration file

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

## Label Configuration

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

## Keyboard Shortcuts Configuration

Configure the keys to activate tools, change selected labels, and more.
Names for shortcut actions are in [constants.ts](https://github.com/Kitware/VolView/blob/main/src/constants.ts#L53) are under the `ACTIONS` variable.

To configure a key for an action, add its action name and the key(s) under the `shortcuts` section. For key combinations, use `+` like `Ctrl+f`.

```yml
shortcuts:
  polygon: "Ctrl+p"
  rectangle: "b"
```

In VolView, show a dialog with the configured keyboard shortcuts by pressing the `?` key.

## Saved Segment Group File Format

Edited segment groups are saved as separate files within session.volview.zip files.  By default the segment group file format is `nii.gz`.

```yml
io:
  segmentGroupSaveFormat: "nii.gz" # default is nii.gz
```

## Automatic Layers and Segment Groups by File Name

When loading multiple image files, VolView can automatically associate related images based on file naming patterns.
For non-DICOM base images, the matching rule is based on the base filename prefix.
The extension must appear anywhere in the filename after splitting by dots,
and the filename must start with the same prefix as the base image (everything before the first dot).

For example, with a base image `patient.nrrd`:

- Layers: `patient.layer.1.pet.nii`, `patient.layer.2.ct.mha`
- Segment groups: `patient.seg.1.tumor.nii.gz`, `patient.seg.2.lesion.mha`

When multiple layers or segment groups match a base image, they are sorted alphabetically by filename and added in that order.

### Segment Groups

Use `segmentGroupExtension` to automatically convert matching non-DICOM images to segment groups.
For example, `myFile.seg.nrrd` becomes a segment group for `myFile.nii`. Defaults to `"seg"`. To disable set to `""`.

```yml
io:
  segmentGroupExtension: "seg" # "seg" is the default
```

### Layering

Use `layerExtension` to automatically layer matching non-DICOM images on top of the base image.
For example, `myImage.layer.nii` is layered on top of `myImage.nii`. Defaults to `"layer"` .To disable set to `""`.

```yml
io:
  layerExtension: "layer" # "layer" is the default
```

For DICOM-specific association rules, explicit `segmentGroups` /
`parentToLayers` session manifest examples, and notes on using DICOM tags versus
file names, see [Loading Layers and Segmentations](./loading_layers_and_segmentations.md).

## Default Window Level

Will force the window level for all loaded volumes.

```yml
windowing:
  level: 100
  width: 50
```
