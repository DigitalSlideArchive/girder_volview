# Girder Plugin for VolView

Open Items in [VolView](https://github.com/Kitware/VolView) with a "Open in VolView" button. The button is located in the top right, on an Item's page.

## Supported Image File Formats

- DICOM `.dcm`
- Nrrd `.nrrd`
- NIFTI `.nii`
- VTK image `.vti`
- And many more. Try dragging and dropping the file(s) on the [VolView Demo Site](https://volview.netlify.app/)

## Configuration

A `.volview_config.yaml` file placed higher in the folder hierarchy configures
the VolView client: view layouts, annotation labels, keyboard shortcuts,
default window/level, segment-group save format, and automatic
layer/segment-group association by file name.

See [Client configuration](./docs/configuration.md), and
[Loading Layers and Segmentations](./docs/loading_layers_and_segmentations.md)
for DICOM-specific association rules.

## Sessions: save / restore

Saving in VolView writes a `session.volview.zip` item next to the data; each
save creates a new one, so older saves remain reopenable. Each open gesture has
one meaning:

- **Open an item / checked images** → fresh, always.
- **Check a `session.volview.zip` item** → exactly that save (back in history).
- **Open a folder (nothing checked)** → the folder's newest save, else its raw images.
- **Open a grouped DICOM row** → the row's newest save, else its images fresh.
- **Refresh (F5) after saving** → reloads the save you just made.

Details, including the launch URL parameters and per-gesture flows, are in
[Sessions](./docs/sessions.md).

## More

- [Session Builder](./session_builder/README.md) — generate VolView sessions
  programmatically with Python, e.g. from analysis pipelines.
- [VolView Radiology CLI](https://github.com/PaulHax/volview-radiology-cli) —
  reference task image used to drive Girder-VolView processing in development
  and end-to-end tests.
- [Building and deploying custom Slicer CLIs](./docs/custom-slicer-clis.md) —
  package VolView analysis tasks, including external-compute adapters, and
  install their images in DSA.
- [Customize file browsing](./docs/customize_file_browsing.md) — group images
  and add metadata columns via `.large_image_config.yaml`.
- [Server administration](./docs/admin.md) — S3 download proxying.

## Development

Dev-stack setup, API endpoints, developing the VolView client against this
plugin, and the backend contract/conformance tests are documented in
[Development](./docs/development.md).
