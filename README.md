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

Edited segment groups are saved as separate files within session.volview.zip files.  By default the segment group file format is `nii.gz`.  Recommended formats: `nrrd`, `nii`, `nii.gz`

```yml
io:
  segmentGroupSaveFormat: "nii.gz" # default is nii.gz
```

### Automatic Segment Groups by File Name

When loading files, VolView can automatically convert images to segment groups
if they follow a naming convention. For example, an image with name `foo.seg.bar`
will be converted to a segment group for a base image named `foo.baz`.  
The `seg` extension is defined by the `io.segmentGroupExtension` key, which takes a
string. Files `[baseFileName].[segmentGroupExtension].bar` will be automatically converted to
segment groups for a base image named `[baseFileName].baz`. The default is `'seg'`.

This will define `myFile.seg.nrrd` as a segment group for a `myFile.nii` base file.

```yml
io:
  segmentGroupExtension: "seg" # "seg" is the default
```

For multiple segmentation images, the first part of the file name up until the first `.` is what is used to match base + segmentation images. For example, this group of file names will match: `my-study.nii`, `my-study.foo.seg.nii`, `my-study.bar.seg.nii`

### Default Window Level

Will force the window level for all loaded volumes.

```yml
windowing:
  level: 100
  width: 50
```

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

3. Configure AWS Cloudfront for the S3 bucket to support HTTP/2 connections

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
