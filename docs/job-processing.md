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
   staged into Girder through the processing API.
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
7. Girder Worker starts the registered container. The CLI fetches its inputs
   from Girder using a short-lived, scoped token, performs the operation, and
   uploads its declared outputs back to the private output folder.
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

Use the
[VolView Radiology CLI repository](https://github.com/PaulHax/volview-radiology-cli)
as the reference implementation. A new operation needs a Slicer Execution Model
XML description, an executable that follows that description, and an entry in
the image's `cli_list.json`. Build and register the image as described in
[development.md](./development.md#radiology-cli-task-image); Girder VolView
derives the task form and output handling from the XML rather than requiring
task-specific backend code.

Choose a unique Docker image name and unique CLI executable/task names. Do not
reuse an image or task identity already registered by HistomicsUI or DIVE-DSA:
all three applications can share the same `slicer_cli_web` task folder, and
registration must not replace another application's entries.

The CLI XML must also declare a category intended for VolView. By default,
Girder VolView admits `Radiology`, `Segmentation`, and `Filtering`, matched
case-insensitively. The allowed set can be changed with
`VOLVIEW_PROCESSING_ALLOWED_CATEGORIES`. HistomicsUI's `HistomicsTK` pathology
CLIs, DIVE-DSA CLIs in other categories, uncategorized CLIs, and malformed
descriptions are excluded from VolView's task list. The same category check is
performed again for task-spec requests and job submission, so an out-of-scope
task cannot be invoked through VolView by guessing its ID.

These rules provide two separate protections: unique image/task identities
avoid collisions in the shared registration catalog, while category scoping
keeps each application's CLIs out of the other application's user interface.
