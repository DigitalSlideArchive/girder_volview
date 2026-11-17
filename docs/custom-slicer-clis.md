# Building and deploying custom Slicer CLIs

Use [Slicer CLI Web](https://github.com/girder/slicer_cli_web) Docker images to
add analysis tasks to Girder VolView. This guide covers VolView-specific
authoring and DSA deployment.

The [VolView Radiology CLI](https://github.com/PaulHax/volview-radiology-cli)
provides segment rulers and statistics. Its separate `volview-cli-examples`
image contains filtering and segmentation examples. Administrators can also
build their own CLI images; this repository is not a required library or base image.

## Add a task

For the reference image, a Python task named `ExampleTask` needs:

1. `ExampleTask/ExampleTask.xml` declaring its parameters and I/O.
2. `ExampleTask/ExampleTask.py` implementing the task and `--xml`.
3. `"ExampleTask": {"type": "python"}` in `examples/cli_list.json`.
4. `COPY ExampleTask /opt/cli/ExampleTask/` in the Dockerfile's `examples` target.

Use unique image/task names because VolView, HistomicsUI, and DIVE-DSA can
share one catalog. Set `<category>` to `Radiology`, `Segmentation`, or
`Filtering`. Write results to the supplied output paths and exit nonzero on
failure.

Slicer CLI Web calls `IMAGE --list_cli` for the manifest and
`IMAGE CLI_NAME --xml` for Slicer Execution Model XML. Other calls execute the
task and propagate its exit code. The reference image uses
`slicer_cli_web.CLIArgumentParser`:

- [`Dockerfile`](https://github.com/PaulHax/volview-radiology-cli/blob/main/Dockerfile) and [`cli_list.py`](https://github.com/PaulHax/volview-radiology-cli/blob/main/cli_list.py): packaging and dispatch.
- [`ExportSegmentStatistics.xml`](https://github.com/PaulHax/volview-radiology-cli/blob/main/ExportSegmentStatistics/ExportSegmentStatistics.xml), [implementation](https://github.com/PaulHax/volview-radiology-cli/blob/main/ExportSegmentStatistics/ExportSegmentStatistics.py), and [manifest](https://github.com/PaulHax/volview-radiology-cli/blob/main/cli_list.json): a complete task.

### Declare inputs and outputs for VolView

Set each parameter's `<channel>` to `input` or `output`. VolView derives scene
behavior from these declarations; authors do not set result intents.

| Channel | Declaration | Behavior |
| --- | --- | --- |
| Input | `<image>` or `<image type="scalar">` | Base image first, then attached layers in declaration order. |
| Input | `<image type="label">` | The base image's segmentation, staged as labelmap files. |
| Input | `<file fileExtensions=".annotations.json">` | All finished rulers, rectangles, and polygons on the base image. |
| Output | `<image>` or `<image type="scalar">` | Loads a new base image. |
| Output | `<image type="label">` | Imports a segmentation onto the input base image. |
| Output | `<file fileExtensions=".annotations.json">` | Adds annotations to the input base image. |
| Output | Other `<file>` | Downloadable under **Details > Files**. |

Unknown image types and opaque file inputs are unsupported. Mapping uses the
XML declaration, not file contents or MIME type; an image declared `type="label"`
is always imported as a segmentation. HistomicsUI may interpret the same XML
differently.

### Reading additional image layers

Scalar inputs bind to the active base and then its attached layers in order.
Labelmap and annotation inputs consume no image slots. Missing required images
block submission; missing optional images are omitted. Binding does not infer
modality from parameter names or filenames.

Make a layer optional with a long flag, empty default, and no index:

```xml
<image type="scalar" reference="_girder_id_">
  <name>inputOverlay</name>
  <label>Overlay Volume</label>
  <channel>input</channel>
  <longflag>inputOverlay</longflag>
  <default></default>
</image>
```

Each input contains one complete volume, including every source file of a
DICOM series. Layers retain their original grids, so the CLI must handle
differing origins, directions, spacing, and dimensions. Use `reference="_girder_id_"` for
[Girder file IDs](#dicom-and-girder-backed-inputs), or `multiple="true"` for
comma-separated local file paths. Multi-file inputs declaring neither are
rejected before job creation. Neither transport assembles a volume; assemble
each image input separately.

Scene outputs always refer to the first scalar image. Its parameter name is
saved with the job so restoration does not depend on JSON key order. Routing
results to later image inputs is unsupported.

### Reading a labelmap input

The client stages the base image's segmentation as compressed `.seg.nrrd`
parts, each containing non-overlapping masks:

- With `multiple="true"`, it stages every part. A singular input stages only
  the first; overlapping segments omitted from it are reported to the user.
- Identify segments by `Segment{N}_Name`. Label values restart at 1 in each
  file and can change between runs.
- Voxels are `uint8`, or `uint16` when a part contains more than 255 segments.

### Writing a labelmap output

Use `.seg.nrrd` to preserve segment names and colors:

```xml
<image type="label" fileExtensions=".seg.nrrd" reference="inputVolume">
  <name>outputLabelmap</name>
  <label>Output Labelmap</label>
  <channel>output</channel>
  <index>1</index>
</image>
```

Write to the supplied path; Girder Worker uploads the file to the job-owned
folder. See [ThresholdSegmentation.xml](https://github.com/PaulHax/volview-radiology-cli/blob/main/ThresholdSegmentation/ThresholdSegmentation.xml)
for a complete declaration.

Results join the base image's segmentation. A matching segment name reuses
its scene color, unless that image already has a mask for the segment; then a
new segment is created with the file's name and color. Empty declared segments
are preserved. Reopening the same job output adds nothing; rerunning the task
adds new results.

For overlapping output segments, write a multi-component volume with one
non-overlapping set per component. `Segment{N}_Layer` is not read.

### The annotations JSON format

Declare vector annotations as `.annotations.json` files:

```xml
<file fileExtensions=".annotations.json" reference="_girder_id_">
  <name>inputAnnotations</name>
  <label>Input Annotations</label>
  <channel>input</channel>
  <index>1</index>
</file>
<file fileExtensions=".annotations.json" reference="inputVolume">
  <name>outputAnnotations</name>
  <label>Output Annotations</label>
  <channel>output</channel>
  <index>2</index>
</file>
```

Coordinates are **world LPS millimeters**, not image indices:

```json
{
  "schemaVersion": 1,
  "space": "LPS",
  "labels": {
    "rulers": { "lesion": { "color": "#ff0000", "strokeWidth": 2 } }
  },
  "tools": {
    "rulers": [{
      "firstPoint": [-30.5, 12.25, -12.5],
      "secondPoint": [18.75, 44, -12.5],
      "frameOfReference": {
        "planeNormal": [0, 0, 1],
        "planeOrigin": [0, 0, -12.5]
      },
      "labelName": "lesion",
      "name": "Ruler"
    }],
    "rectangles": [],
    "polygons": []
  }
}
```

Format constraints:

- `schemaVersion` is `1`; `space` is `"LPS"`. Empty tool arrays are valid.
- Rulers and rectangles use `firstPoint` and `secondPoint`; rectangle points
  are opposite corners along the image's in-plane axes. Polygons use at least
  three `points`. Use polygons for rotated boxes.
- Every tool requires `frameOfReference` containing exactly `planeNormal` and
  `planeOrigin`. Normals must be finite and nonzero; consumers normalize them.
- Optional tool fields are `slice`, `frame`, `labelName`, `name`, and
  string-to-string `metadata`. Other fields, including session IDs and styles,
  are rejected. `slice` must be finite; `frame` must be a nonnegative JavaScript
  safe integer.
- Each tool kind has its own `labels` namespace. Nonempty `labelName` values
  must resolve there. Labels allow only string `color`, numeric `strokeWidth`,
  and string `fillColor`; `__proto__` is reserved in labels and metadata.
- Colors accept hex values and CSS keywords, not `rgb()` or `hsl()`. Invalid
  colors produce a warning and retain the scene color. `fillColor` is accepted
  but ignored; rectangles remain unfilled.

For output, normalized plane normals must match a referenced image axis
within the `vec3.equals` tolerance (`1e-6`). Oblique planes reject the whole
result. Echoing an input's `frameOfReference` is usually simplest.
`planeOrigin` determines placement: between-slice origins are rejected;
out-of-bounds slice origins are accepted. `slice` and `frame` are advisory,
and an out-of-range cine frame is dropped. Loading the same output twice
adds no duplicate tools.

The normative files in the `volview` package are:

- `backend-contract/generated/annotations-file.schema.json`
- `backend-contract/processing/annotations.ts`
- `backend-contract/fixtures/wire/annotations-file.json`

See [Backend contract and tests](./development.md#backend-contract-and-tests)
for their location, [annotations-measure.xml](../tests/slicer_xml/annotations-measure.xml)
for a complete declaration, and [RulerToRectangle](https://github.com/PaulHax/volview-radiology-cli/tree/main/RulerToRectangle)
for an implementation. This format differs from the session-manifest format
written by [`session_builder.add_annotation`](../session_builder/README.md);
do not interchange them.

### DICOM and Girder-backed inputs

Use `reference="_girder_id_"` to receive comma-separated Girder file IDs:

```xml
<parameters>
  <label>Inputs</label>
  <image reference="_girder_id_">
    <name>inputVolume</name>
    <label>Input Volume</label>
    <channel>input</channel>
    <index>0</index>
  </image>
</parameters>
<parameters advanced="true">
  <label>Girder API</label>
  <string>
    <name>girderApiUrl</name>
    <longflag>girderApiUrl</longflag>
    <default></default>
  </string>
  <string>
    <name>girderToken</name>
    <longflag>girderToken</longflag>
    <default></default>
  </string>
</parameters>
```

Girder VolView supplies a one-day token for the submitting user, scoped to
data read/write; Slicer CLI Web injects it and the API URL into the reserved
parameters.

The reference [`girder_input`](https://github.com/PaulHax/volview-radiology-cli/blob/main/volview_cli_base/girder_input.py)
downloads files, and [`assemble`](https://github.com/PaulHax/volview-radiology-cli/blob/main/volview_cli_base/assemble.py)
orders DICOM slices by metadata. Do not rely on ID or filename order. See
[OtsuSegmentation.xml](https://github.com/PaulHax/volview-radiology-cli/blob/main/OtsuSegmentation/OtsuSegmentation.xml)
and [How image inputs become CLI files](./job-processing.md#how-image-inputs-become-cli-files).

## Dispatching to an external compute service

A CLI can forward the injected Girder URL/token, file IDs, and task parameters
to a service that downloads inputs directly. Wait for completion, write each
result to its declared output path, and exit nonzero on failure. Configure
service credentials separately and ensure the service can reach Girder.

Cancel downstream work when the container terminates; set downstream timeouts
and retry limits. For progress, print `<filter-progress>` and `<filter-comment>`
tags as shown in [ExampleProgress](https://github.com/girder/slicer_cli_web/tree/master/small-docker/ExampleProgress).

## Verify the image

Using the reference Dockerfile:

```sh
IMAGE=registry.example.org/team/example-cli:0.1.0
docker build --target examples -t "$IMAGE" .
docker run --rm "$IMAGE" --list_cli
docker run --rm "$IMAGE" ExampleTask --xml
docker build --target test .
```

In DSA, check catalog visibility, input bindings, scene results/downloads,
failed-job reporting, isolation of concurrent outputs, and cancellation of
any downstream service.

## Register the image in DSA

Add the image to the provision YAML and rerun provisioning:

```yaml
slicer-cli-image:
  - ghcr.io/paulhax/volview-radiology-cli:0.2.1
```

The host uses an existing local image or pulls it when the tag is missing.
Publish immutable versions; use `slicer-cli-image-pull` to refresh a mutable
tag. The admin page also supports interactive import. For private images,
configure [registry authentication](https://github.com/DigitalSlideArchive/digital_slide_archive/blob/master/devops/dsa/README.rst#using-private-docker-registries-for-cli-images).

Reimporting a tag removes tasks omitted from its new manifest. When switching
tags, remove the retired registration to remove its tasks. See
[deployment helpers](./development.md#slicer-cli-images).

## Further reading

- [Slicer CLI Web Docker CLI specification](https://github.com/girder/slicer_cli_web#docker-clis)
- [Slicer CLI module overview](https://slicer.readthedocs.io/en/latest/developer_guide/module_overview.html#command-line-interface-cli) and [Execution Model parameters](https://slicer.readthedocs.io/en/latest/user_guide/modules/executionmodeltour.html)
- [Job processing design](./job-processing.md)
- [DSA deployment](https://github.com/DigitalSlideArchive/digital_slide_archive/tree/master/devops/dsa)
