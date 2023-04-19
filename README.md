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

## Endpoints

- POST item/:id/volview -> upload file to Item with cookie authentication
- GET item/:id/volview -> download latest session.volview.zip
- GET item/:id/volview/datasets -> download all files in item except the `*.volview.zip`

## Example Saving Roundtrip flow

1. User clicks Open in VolView for Item - Plugin checks if `*volview.zip` file exists in Item, finds none:
   Opens VolView with file download url `item/:id/volview/datasets`
1. VolView opens, fetches from `item/:id/volview/datasets`, receives zip of all files in Item except files ending in `*volview.zip`
1. In VolView, User clicks the Save button - VolView POSTs session.volview.zip to `item/:id/volview`
1. girder_volview plugin saves new session.volview.zip in Item.
1. User clicks Open in VolView for Item - Plugin finds a `*volview.zip` in the Item. Opens VolView with file download URL pointing to `item/:id/volview`
1. VolView opens, fetches from `item/:id/volview`, receives most recently created `*volview.zip` file in Item.

VolView creates a new session.volview.zip file in the Girder Item every time the Save button is clicked.
