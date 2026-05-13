# Loading Layers and Segmentations

VolView can load a base image with two overlay types:

- Layer: scalar image data such as PET, perfusion, probability, or heat maps.
- Segmentation: integer label maps shown in the Segment Groups panel.

The overlay must overlap the base image in physical space. VolView resamples it
into the base image space.

## Automatic Filename Matching

By default, use `.seg.` for segmentations and `.layer.` for layers:

```text
patient01.ct.nii.gz          # base
patient01.seg.tumor.nii.gz   # segmentation
patient01.layer.pet.nii.gz   # layer
```

For NIfTI/NRRD/MHA files:

- The base prefix is the text before the first dot.
- Overlays must start with the same prefix.
- The configured extension must appear as a dot-separated token.
- Multiple matches load alphabetically.

To change the tokens, put `.volview_config.yaml` at or above the Girder folder:

```yaml
io:
  segmentGroupExtension: "seg"
  layerExtension: "layer"
```

Set either value to `""` to disable that automatic conversion.

## DICOM Matching

VolView groups DICOM instances into volumes, then chooses a preferred base.

- If the selected base is CT, the first PT volume in the same
  `StudyInstanceUID` is added as a layer.
- DICOM SEG volumes in the same `StudyInstanceUID` become segmentations.
- Non-DICOM overlays match a DICOM base by `SeriesNumber`.

Example:

```text
CT SeriesNumber = 3
3.seg.tumor.nii.gz
3.layer.pet.nii.gz
```

## Explicit Manifests

Use a VolView session manifest JSON for lower level control. [Session builder README](../session_builder/README.md)
