# Building and deploying custom Slicer CLIs

Use [Slicer CLI Web](https://github.com/girder/slicer_cli_web) Docker images to
add analysis tasks to Girder VolView. This guide covers VolView-specific
authoring and DSA deployment.

The [VolView Radiology CLI](https://github.com/PaulHax/volview-radiology-cli)
is example and test infrastructure with scalar-image, labelmap, and multi-input
tasks. Administrators build and publish their own CLI images; this repository
is not a required library or base image.

Related documentation:

- [Slicer CLI Web Docker CLI specification](https://github.com/girder/slicer_cli_web#docker-clis)
- [3D Slicer CLI module overview](https://slicer.readthedocs.io/en/latest/developer_guide/module_overview.html#command-line-interface-cli)
- [3D Slicer Execution Model parameters](https://slicer.readthedocs.io/en/latest/user_guide/modules/executionmodeltour.html)
- [DSA deployment](https://github.com/DigitalSlideArchive/digital_slide_archive/tree/master/devops/dsa)

## What Girder VolView adds

Slicer CLI Web registers and runs images. Girder VolView filters the shared task
catalog, builds Jobs forms from CLI XML, validates submissions, owns output
locations, and applies completed results to the scene.

Requirements:

1. Use unique image and task names. VolView, HistomicsUI, and DIVE-DSA can share
   one Slicer CLI Web catalog.
2. Set `<category>` to `Radiology`, `Segmentation`, or `Filtering`.
3. Declare input and output types for VolView's scene mapping. See
   [Add a task](#add-a-task).
4. Write results to the output paths passed to the executable.
5. Exit nonzero on failure.

See [Job processing design](./job-processing.md) for the complete execution flow.

## Inspect the example image

The example [`Dockerfile`](https://github.com/PaulHax/volview-radiology-cli/blob/main/Dockerfile) packages the image, and
[`cli_list.py`](https://github.com/PaulHax/volview-radiology-cli/blob/main/cli_list.py) implements its discovery and dispatch entrypoint.

```sh
git clone https://github.com/PaulHax/volview-radiology-cli.git
cd volview-radiology-cli
docker build -t volview-radiology-cli:local .
docker run --rm volview-radiology-cli:local --list_cli
docker run --rm volview-radiology-cli:local ThresholdSegmentation --xml
docker build --target test .
```

Slicer CLI Web discovers an image through two entrypoint calls:

```text
docker run IMAGE --list_cli
docker run IMAGE CLI_NAME --xml
```

The first returns the CLI manifest. The second returns a task's Slicer
Execution Model XML. Ordinary calls run the selected executable and propagate
its exit code.

## Add a task

A Python task named `ExampleTask` needs:

1. `ExampleTask/ExampleTask.xml` with its category, parameters, and I/O.
2. `ExampleTask/ExampleTask.py` with `--xml` support and the implementation.
3. `"ExampleTask": {"type": "python"}` in `cli_list.json`.

The reference image uses `slicer_cli_web.CLIArgumentParser`. Other languages
are valid if the entrypoint contract and XML match the command line.

[PR #4](https://github.com/PaulHax/volview-radiology-cli/pull/4) shows these
parts together: the task's
[`RegionOfInterestReport.xml`](https://github.com/PaulHax/volview-radiology-cli/blob/main/RegionOfInterestReport/RegionOfInterestReport.xml),
[`RegionOfInterestReport.py`](https://github.com/PaulHax/volview-radiology-cli/blob/main/RegionOfInterestReport/RegionOfInterestReport.py), and [`cli_list.json`](https://github.com/PaulHax/volview-radiology-cli/blob/main/cli_list.json) registration.

### Declare inputs and outputs for VolView

VolView uses Slicer XML to build the form and connect job data to the scene.
Users and CLI authors do not set intents. Girder VolView derives each output
intent from its XML declaration using the rules below.

| Direction | Slicer XML                                                      | VolView behavior                                                       |
| --------- | --------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Input     | `<image channel="input">`; omitted `type` defaults to `scalar`  | Uses the selected base scalar image.                                   |
| Input     | `<image channel="input" type="label">`                          | Uses the selected segment group's labelmap.                            |
| Output    | `<image channel="output">`; omitted `type` defaults to `scalar` | `add-base-image`: loads the output as a new base image.                |
| Output    | `<image channel="output" type="label">`                         | `add-segment-group`: adds the output labelmap to the input base image. |
| Output    | `<file channel="output">`                                       | No scene intent; downloadable under **Details > Files**.               |

This mapping is VolView-specific; HistomicsUI can interpret the same CLI
differently. Unknown image types are rejected.

Example labelmap output:

```xml
<image type="label" fileExtensions=".seg.nrrd" reference="inputVolume">
  <name>outputLabelmap</name>
  <label>Output Labelmap</label>
  <channel>output</channel>
  <index>1</index>
</image>
```

Use `.seg.nrrd` to carry segment names and colors. Write the file to the output
path passed to the CLI; Girder Worker uploads it to the job-owned folder.

See [`ThresholdSegmentation.xml`](https://github.com/PaulHax/volview-radiology-cli/blob/main/ThresholdSegmentation/ThresholdSegmentation.xml)
for a complete scalar-image-to-labelmap declaration.

### DICOM and Girder-backed inputs

To receive every file in a DICOM series, pass Girder IDs instead of staging one
file:

```xml
<image reference="_girder_id_">
  <name>inputVolume</name>
  <label>Input Volume</label>
  <channel>input</channel>
  <index>0</index>
</image>
```

See [`OtsuSegmentation.xml`](https://github.com/PaulHax/volview-radiology-cli/blob/main/OtsuSegmentation/OtsuSegmentation.xml)
for the complete input and credential declarations.

Declare the reserved parameters that Slicer CLI Web populates at run time:

```xml
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

The input argument is a comma-separated list of file IDs. The example's
[`girder_input`](https://github.com/PaulHax/volview-radiology-cli/blob/main/volview_cli_base/girder_input.py)
downloads them, and
[`assemble`](https://github.com/PaulHax/volview-radiology-cli/blob/main/volview_cli_base/assemble.py)
sorts and assembles the DICOM series from metadata.
Do not rely on ID or filename order.

Girder VolView mints a new one-day token for the submitting user, scoped to
data read and write. Slicer CLI Web injects that token and the Girder API URL
into the declared parameters.

See [How image inputs become CLI files](./job-processing.md#how-image-inputs-become-cli-files)
for the complete VolView-to-container translation.

## Dispatching to an external compute service

The CLI container can relay Girder inputs to a GPU or
workflow service without downloading and re-uploading them:

1. Submit the injected Girder API URL, per-job token, file IDs, and task parameters.
2. Let the workflow service download the inputs directly from Girder.
3. Wait for completion and report progress.
4. Download each result to its declared CLI output path.
5. Validate results and exit zero; exit nonzero on failure.

Configure the workflow endpoint and its own credentials in the deployment.
Girder access uses the injected per-job token; the workflow service only needs
network access to the Girder API.

Handle container termination by cancelling downstream work. Set downstream
timeouts and retry limits; otherwise the outer job waits for the adapter.

For progress reporting, print `<filter-progress>` and `<filter-comment>` tags.
See Slicer CLI Web's
[`ExampleProgress`](https://github.com/girder/slicer_cli_web/tree/master/small-docker/ExampleProgress).

## Verify the image

```sh
IMAGE=registry.example.org/team/example-cli:0.1.0
docker build -t "$IMAGE" .
docker run --rm "$IMAGE" --list_cli
docker run --rm "$IMAGE" ExampleTask --xml
docker build --target test .
```

In a development DSA deployment, confirm:

- the task appears only in the intended application;
- image and labelmap controls accept the correct sources;
- outputs load into the scene or appear under **Details > Files**;
- invalid parameters and failures produce failed jobs;
- concurrent jobs keep same-named outputs separate; and
- cancellation stops downstream work when applicable.

## Publish and install the image in DSA

Publish an immutable version accessible to the DSA Docker host. The example
[`Dockerfile`](https://github.com/PaulHax/volview-radiology-cli/blob/main/Dockerfile)
shows one way to package the entrypoint and tasks. Push your own image under an
immutable registry tag.

After publishing, add a container image reference to the DSA provision
YAML. This is what it would look like for [`volview-radiology-cli`](https://github.com/PaulHax/volview-radiology-cli/):

```yaml
slicer-cli-image:
  - ghcr.io/paulhax/volview-radiology-cli:0.1.0
```

`slicer-cli-image` pulls only when the tag is absent locally. Use
`slicer-cli-image-pull` only for intentionally mutable tags. Re-run the normal
provisioning command to pull and register the tasks.

The Slicer CLI Web admin page can import an image interactively, but provision
YAML is the repeatable installation path. For private images, configure
[DSA registry authentication](https://github.com/DigitalSlideArchive/digital_slide_archive/blob/master/devops/dsa/README.rst#using-private-docker-registries-for-cli-images).

For local development without a registry, see
[Server administration](./admin.md#local-reference-image).
