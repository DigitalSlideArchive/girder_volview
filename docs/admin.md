# Server administration

## Job processing

[Register Slicer CLI images](./custom-slicer-clis.md#register-the-image-in-dsa)
to add tasks to VolView. See [Job processing design](./job-processing.md) for
execution flow and category scoping in a shared Slicer CLI Web deployment.

## Girder 5: deletions run in the local worker

Girder 5 runs some deletions as background tasks on Girder Worker's `local`
queue instead of in the web server:

- `DELETE /folder/{id}`
- `DELETE /folder/{id}/contents`
- `DELETE /collection/{id}`

A Girder Worker process must be consuming the `local` queue. Without one, these
requests fail with `503 No local worker is available to process this request`.

That worker process does not load Girder server plugins, so the VolView
plugin's event handlers are not registered there. One of them matters for
deletion: the `model.folder.remove` handler that deletes a job's record when its
private output folder is removed. When one of the deletions above removes a job
output folder, the `volview-jobs` container, or a folder or collection holding
either, the folders are deleted but the job records are left behind. Those jobs
stay in VolView's job history with their results missing.

This is not handled yet. Fixing it needs the VolView handlers registered in the
worker process too. large_image's Girder 5 plugin shows how: it declares a
`girder_worker_plugins` entry point
(`girder_large_image.girder_worker_plugin:LargeImageWorkerPlugin`). Girder Worker
instantiates every such entry point at startup, and that class's constructor
calls `events.bind` for the model events large_image needs in the worker. VolView
needs the same, binding `outputs._cascadeDeleteFolderOwnedJob` to
`model.folder.remove` and `outputs._cascadeDeleteJobOwnedResources` to
`model.job.remove`.

What still works, because it runs in the web server:

- The check that refuses to delete a folder, collection, user or resource
  containing a running job. It runs before the task is queued.
- Deleting a job (from VolView or through Girder's job routes). It still deletes
  the job's output folder and results.
- `DELETE /user/{id}` and `DELETE /resource`, which Girder 5 still runs
  synchronously.

The plugin's tests run these tasks eagerly, inside the test process where the
plugin is loaded, so they do not catch this gap.

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
