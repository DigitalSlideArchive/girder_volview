# Job processing design

This branch adds server-backed processing to VolView's Jobs tab. The
Girder VolView plugin presents a small, VolView-specific API while continuing to
use `slicer_cli_web`, Girder Jobs, Girder Worker, and containerized Slicer CLIs
for task registration and execution.

At a high level, the pieces are:

- **VolView** discovers tasks, renders their parameters, submits work, polls job
  status, and applies completed results to the active scene.
- **Girder VolView** translates between VolView's processing contract and the
  Slicer CLI model. It validates submissions and owns the job-to-output
  relationship.
- **`slicer_cli_web`** registers the CLIs supplied by container images and
  creates the Docker-backed Girder jobs.
- **Girder Worker** runs the selected container and transfers its inputs and
  outputs through Girder.

## Execution flow

When a user submits a task, the following happens:

1. VolView gets the available task list for the folder that provided its launch
   context. The backend reads the accessible `slicer_cli_web` catalog and
   returns only CLIs in the configured VolView processing categories.
2. VolView requests the selected task's specification. Girder VolView parses
   the Slicer Execution Model XML and translates it into the task and parameter
   shapes understood by VolView.
3. VolView submits the selected task ID and parameter values. Inputs already in
   Girder are represented by file handles; client-generated data is first
   staged into the launch folder's server-owned `volview-jobs` container through
   the processing API, keeping temporary working items out of the source-data
   folder.
4. Girder VolView authenticates the user, checks folder access, confirms that
   the task is still in VolView's allowed scope, and validates every submitted
   value against the CLI declaration. Reserved credentials, undeclared
   parameters, and caller-selected output locations are rejected.
5. The backend creates a private output folder for this submission. It resolves
   input handles using the submitting user's permissions, copies transient
   staged inputs into the job-owned folder, generates safe output names, and
   forces every declared output into that folder.
6. The backend asks `slicer_cli_web` to create the container job. The job record
   is created with its launch context, submitted parameters, declared outputs,
   transient inputs, and owned output-folder ID before the task is published to
   Girder Worker.
7. Girder Worker starts the registered container. Normal Slicer CLI Web image
   inputs are downloaded or mounted as worker-local paths. Inputs declared with
   `reference="_girder_id_"` instead reach the CLI as Girder file IDs; the CLI
   can fetch them with the short-lived, scoped token or relay them to an
   external workflow service. The CLI performs the operation and uploads its
   declared outputs back to the private output folder.
8. As each upload is finalized, Girder VolView associates the file with the job
   using the job-owned folder and the worker's declared output reference. It
   does not correlate results by filename, so concurrent jobs producing the
   same filename cannot cross-associate their results.
9. VolView polls the job-addressed status and results endpoints. The backend
   projects Girder's job states into the VolView contract and returns completed
   files as result records with declarative application intents, such as adding
   a base image or segment group.
10. VolView applies ready results to the scene. The completed job remains in the
    user's folder-scoped history and can be reopened later. Transient inputs are
    cleaned up when execution settles; deleting a terminal job also deletes its
    owned output folder and results.

## Adding a Slicer CLI

See [Building and deploying custom Slicer CLIs](./custom-slicer-clis.md) for a
worked recipe based on the VolView Radiology CLI, including DICOM inputs,
VolView result types, external-compute adapters, image publication, and durable
installation in a DSA deployment.

## How image inputs become CLI files

VolView does not upload a reconstructed volume when the selected image already
comes from Girder. It submits an input descriptor containing the image's source
file handles. A DICOM series has one URI per slice; a single-file NRRD, NIfTI,
MHA, or multiframe DICOM normally has one:

```json
{
  "type": "image",
  "format": "dicom-series",
  "uris": [
    "/api/v1/file/<slice-1-id>/proxiable/1.dcm",
    "/api/v1/file/<slice-2-id>/proxiable/2.dcm"
  ]
}
```

Girder VolView then prepares the container argument:

1. The [input resolver](../girder_volview/backend/inputs.py) accepts only file
   handles minted by this Girder server, recovers every file ID, and checks the
   submitting user's read access. A DICOM series is authorized in batched
   queries rather than one query per slice.
2. The [submission translator](../girder_volview/backend/submit.py) preserves
   the URI list and joins the resolved IDs into one argument:

   ```text
   --inputVolume <slice-1-id>,<slice-2-id>,<slice-3-id>
   ```

3. Declare the input with `reference="_girder_id_"` to preserve that argument:

   ```xml
   <image reference="_girder_id_">
     <name>inputVolume</name>
     <channel>input</channel>
   </image>
   ```

   This special reference tells
   [`slicer_cli_web`](https://github.com/girder/slicer_cli_web/blob/master/slicer_cli_web/cli_utils.py)
   not to treat the value as one Girder file and replace it with a worker-local
   path. Without it, the normal Slicer CLI Web `<image>` binding accepts one
   file ID and uses a Girder Worker transform to download or mount that file.
4. Slicer CLI Web leaves the comma-separated value intact and injects the
   Girder API URL and the job's short-lived token.
5. The CLI decides how to resolve the IDs. It can download the files itself or
   relay the IDs and credentials to an external workflow service. A CLI that
   assembles DICOM locally must group and order slices from DICOM metadata, not
   from filenames, URI order, or Girder-ID order.

### Worked example

Administrators build and publish their own CLI images. The VolView Radiology
CLI is example and test infrastructure, not a required library or base image.
Its merged [region-of-interest report PR](https://github.com/PaulHax/volview-radiology-cli/pull/4)
is a compact example of adding one task:

- [`RegionOfInterestReport.xml`](https://github.com/PaulHax/volview-radiology-cli/blob/main/RegionOfInterestReport/RegionOfInterestReport.xml)
  declares the labelmap input, CSV file output, Girder parameters, and task
  options.
- [`RegionOfInterestReport.py`](https://github.com/PaulHax/volview-radiology-cli/blob/main/RegionOfInterestReport/RegionOfInterestReport.py)
  implements the executable entry point and writes the declared output path.
- [`cli_list.json`](https://github.com/PaulHax/volview-radiology-cli/blob/main/cli_list.json)
  registers the executable for container discovery.
- [`test_roi_report.py`](https://github.com/PaulHax/volview-radiology-cli/blob/main/tests/test_roi_report.py)
  and [`test_roi_report_itk.py`](https://github.com/PaulHax/volview-radiology-cli/blob/main/tests/test_roi_report_itk.py)
  exercise its report behavior.

Use the same XML, executable, registration, and test structure when creating a
new task in your own image.
