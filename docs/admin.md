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

That worker process does not load Girder server plugins, so the VolView server
plugin's event handlers are not registered there. One of them matters for
deletion: the `model.folder.remove` handler that deletes a job's record when its
private output folder is removed (along with the `model.job.remove` handler it
triggers, which cleans up the job's staged inputs).

To cover this, the package also registers a Girder Worker plugin, through the
`girder_worker_plugins` entry point
`volview = girder_volview.worker_plugin:VolViewWorkerPlugin`. Girder Worker
instantiates every such entry point at startup, and VolView's binds those two
deletion handlers. large_image's Girder 5 plugin does the same for its own
handlers.

**Install `girder_volview` in the worker's Python environment.** If it is
missing there, the worker never finds the entry point. Deleting a job output
folder, the `volview-jobs` container, or a folder or collection holding either
then removes the folders but leaves the job records behind, and those jobs stay
in VolView's job history with their results missing.

What runs in the web server either way:

- The check that refuses to delete a folder, collection, user or resource
  containing a running job. It runs before the task is queued.
- Deleting a job (from VolView or through Girder's job routes), which also
  deletes the job's output folder and results.
- `DELETE /user/{id}` and `DELETE /resource`, which Girder 5 still runs
  synchronously.

`tests/test_worker_plugin.py` runs Girder's folder-delete task without the
VolView server plugin loaded, with and without the worker plugin.

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
