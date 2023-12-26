# Girder Plugin for VolView

Open Items in [VolView](https://github.com/Kitware/VolView) with a "Open in VolView" button. The button is located in the top right, on an Item's page.

## Supported Files

VolView tries to load all files in a Girder Item.
`.zip` files, and all `.zip` files they contain, are unzipped and VolView will load all resulting files.

### Supported Image File Formats

- DICOM (.dcm)
- Nrrd (.nrrd)
- NIFTI
- VTK image (.vti)
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
# If True, the Girder server will proxy file download requests from VolView
# to the S3 assetstore. This will use more server bandwidth.
# If False, VolView client requests to download files are redirected to S3.
proxy_assetstores = True
```

## API Endpoints

- POST item/:id/volview -> upload file to Item with cookie authentication
- GET item/:id/volview -> download latest session.volview.zip
- GET item/:id/volview/manifest -> download JSON with URLs to all files in item except the `*.volview.zip`
- GET file/:id/proxiable/:name -> download a file with option to proxy 
- Deprecated: GET item/:id/volview/datasets -> download all files in item except the `*.volview.zip`

## Example Saving Roundtrip flow

1. User clicks Open in VolView for Item - Plugin checks if `*volview.zip` file exists in Item, finds none:
   Opens VolView with file download url `item/:id/volview/datasets`
1. VolView opens, fetches from `item/:id/volview/datasets`, receives zip of all files in Item except files ending in `*volview.zip`
1. In VolView, User clicks the Save button - VolView POSTs session.volview.zip to `item/:id/volview`
1. girder_volview plugin saves new session.volview.zip in Item.
1. User clicks Open in VolView for Item - Plugin finds a `*volview.zip` in the Item. Opens VolView with file download URL pointing to `item/:id/volview`
1. VolView opens, fetches from `item/:id/volview`, receives most recently created `*volview.zip` file in Item.

VolView creates a new session.volview.zip file in the Girder Item every time the Save button is clicked.

## VolView Client Update Steps

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

Then clean docker images

```
docker rm dsa-plus_girder_1 dsa-plus_worker_1 dsa-plus_rabbitmq_1 dsa-plus_memcached_1 dsa-plus_mongodb_1
```

Start containers again

```
DSA_USER=$(id -u):$(id -g) docker-compose -f ../dsa/docker-compose.yml -f docker-compose.override.yml -p dsa-plus up
```

Bash into girder container

```
DSA_USER=$(id -u):$(id -g) docker-compose -f ../dsa/docker-compose.yml -f docker-compose.override.yml -p dsa-plus exec girder bash
```

On Bash terminal, install your mounted local dev version of plugin.

```
cd /opt/girder_volview/ && pip install -e .
```

For the Girder plugin watch and rebuild feature, I must stop and start
containers again. Then on Girder Bash prompt run

```
girder build --dev --watch-plugin volview
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
VITE_REMOTE_SERVER_URL= VITE_ENABLE_REMOTE_SAVE=true npm run build -- --base=/static/built/plugins/volview
```
