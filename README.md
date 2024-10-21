# Girder Plugin for VolView

Open Items in [VolView](https://github.com/Kitware/VolView) with a "Open in VolView" button. The button is located in the top right, on an Item's page.

## Supported Files

VolView tries to load all files in a Girder Item.
`.zip` files, and all `.zip` files they contain, are unzipped and VolView will load all resulting files.

### Supported Image File Formats

- DICOM `.dcm`
- Nrrd `.nrrd`
- NIFTI `.nii`
- VTK image `.vti`
- And many more. Try dragging and dropping the file(s) on the [VolView Demo Site](https://volview.netlify.app/)

## Layers of Images

To overlay PET and CT images, place all image files in one Girder Item.
VolView will show the PET and CT images as separate "volumes".
First load the base volume, say the CT one. Then click the "Add Layer" icon on the overlay image, probably the PET one.

The overlaid image is "resampled" to match the physical and pixel space of the base image.  
If there is no overlap in physical space as gleaned from the images' metadata, the overlay won't work.

## Client Configuration file

Add a `.volview_config.yaml` file higher in the folder hierarchy. Example file:

```yml
layout:
  activeLayout: "Axial Only"
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
layout:
  activeLayout: "Axial Only"
```

Result

```yml
shortcuts:
  polygon: "Ctrl+p"
  rectangle: "b"
layout:
  activeLayout: "Axial Only"
```

### Layout Configuration

To set the initial view, add a `layout: activeLayout` section to the `.volview_config.yaml` file.

```yml
layout:
  # options: Axial Only, Axial Primary, 3D Primary, Quad View, 3D Only
  activeLayout: "Axial Only"
```

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

Edited segment groups are saved as separate files within session.volview.zip files.  By default the segment group file format is `.vti`.  We can change the format to `nrrd`, `nii`, `nii.gz` or `hdf5`

```yml
io:
  segmentGroupSaveFormat: "nrrd"
```

### Automatic Segment Groups by File Name

When loading files, VolView can automatically convert images to segment groups
if they follow a naming convention. For example, an image with name like `foo.seg.bar`
will be converted to a segment group for a base image named like `foo.baz`.  
The `segmentation` extension is defined by the `io.segmentGroupExtension` key, which takes a
string. Files `[baseFileName].[segmentGroupExtension].bar` will be automatically converted to
segment groups for a base image named `[baseFileName].baz`. The default is `'seg'`.

This will define `myFile.seg.nrrd` as a segment group for a `myFile.nii` base file.

```yml
io:
  segmentGroupExtension: "seg" # "seg" is the default
```

## Table View via Grider Plugin Configuration File

![image](https://github.com/DigitalSlideArchive/girder_volview/assets/16823231/9ab0f04d-9103-431a-ab22-cbf87ee760e2)

To show DICOM tags in a table view, add a `.large_image_config.yaml` file higher in the Girder folder hierarchy. When a DICOM file is imported/uploaded, its DICOM tags are saved on the Item metadata under the top level `dicom` key.

More information on `.large_image_config.yaml` here:
https://girder.github.io/large_image/girder_config_options.html#large-image-config-yaml

Example `.large_image_config.yaml` file:

```yml
# If present, show a table with column headers in item lists
itemList:
  # Show these columns in order from left to right.  Each column has a
  # "type" and "value".  It optionally has a "title" used for the column
  # header, and a "format" used for searching and filtering.  The "label",
  # if any, is displayed to the left of the column value.  This is more
  # useful in an grid view than in a column view.
  columns:
    - # The "record" type is from the default item record.  The value is
      # one of "name", "size", or "controls".
      type: record
      value: name
    - type: record
      value: size
    - # The "metadata" type is taken from the item's "meta" contents.  It
      # can be a nested key by using dots in its name.
      type: metadata
      value: dicom.Modality
      title: Modality
    - type: metadata
      value: dicom.BodyPartExamined
      title: Body Part Examined
    - type: metadata
      value: dicom.StudyDate
      title: Study Date
    - type: metadata
      value: dicom.StudyDescription
      title: Study Description
    - type: metadata
      value: dicom.SeriesDescription
      title: Series Description
    - type: metadata
      value: dicom.ManufacturerModelName
      title: Manufacturer Model Name
    - type: metadata
      value: dicom.StudyInstanceUID
      title: Study Instance UID
    - type: metadata
      value: dicom.SeriesInstanceUID
      title: Series Instance UID
```

## CORS Error Workaround by Proxying Assetstores

VolView will error if it loads a file from a S3 bucket asset store without some
[CORS configuration](https://girder.readthedocs.io/en/stable/user-guide.html#s3).
To workaround needing to change the bucket configuration, the Girder admin can
change the global [Girder configuration](https://girder.readthedocs.io/en/stable/configuration.html).
Adding a `[volview]` section with a `proxy_assetstores = True` option routes VolView's
file download requests through the Girder server, rather than redirecting directly to the S3 bucket.

```
[volview]
# Workaround CORS configuration errors in S3 assetstores.
# If True, the Girder server will proxy file download requests from
# VolView clients to the S3 assetstore. This will use more server bandwidth.
# If False, VolView client requests to download files are redirected to S3.
# Defaults to True.
proxy_assetstores = True
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

## Development

Get this running https://github.com/DigitalSlideArchive/digital_slide_archive/tree/master/devops/with-dive-volview

In the `provision.divevolview.yaml` file, add some `volumes` pointing to this girder plugin and optionally
a VolView repo checkout. Example:

```yaml
services:
  girder:
    volumes:
      - ../with-dive-volview/provision.divevolview.yaml:/opt/digital_slide_archive/devops/dsa/provision.yaml
      - ../../../girder-volview:/opt/girder_volview
      - ~/src/volview-stuff/VolView:/opt/volview-package
```

Comment out the pip install of this plugin here: https://github.com/DigitalSlideArchive/digital_slide_archive/blob/master/devops/with-dive-volview/provision.divevolview.yaml#L3

To install volume mapped girder-volview plugin and incorporate changes as files are edited, add this to the `shell` section of the provision.yaml:

```yaml
shell:
  - cd /opt/girder_volview/ && pip install -e .
  - (sleep 30 && girder build --dev --watch-plugin volview)&
```

### Develop VolView client

Change the directory the Webpack copy plugin pulls from to your mounted volume with local VolView build
In here: https://github.com/PaulHax/girder_volview/blob/main/girder_volview/web_client/webpack.helper.js#L9-L14

```js
new CopyWebpackPlugin([
  {
    from: "/opt/volview-package/dist",
    to: config.output.path,
    toType: "dir",
  },
]);
```

Then build VolView with the right flags:
https://github.com/PaulHax/girder_volview/blob/main/volview-girder-client/buildvolview.sh#L14C2-L14C108

```
VITE_ENABLE_REMOTE_SAVE=true npm run build -- --base=/static/built/plugins/volview
```

### VolView Client Update Steps

Change VolView commit SHA in `volview-girder-client/buildvolview.sh`

Build VolView client with Girder specific CLI arguments:

```sh
cd volview-girder-client
source buildvolview.sh
```

Increase version in `volview-girder-client/package.json`.

Publish built VolView `dist` directory to NPM:

```sh
cd volview-girder-client
npm publish
```

Update volview-girder-client version in `./grider_volview/web_client/package.json`

To test new client: push up changes to a new branch on GitHub. Change `provision.divevolview.yaml` to point to your branch like this: `git+https://github.com/PaulHax/girder_volview@new-branch`.
Rebuild DSA Girder docker image.
