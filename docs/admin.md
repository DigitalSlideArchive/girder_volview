# Server administration

## Job processing

See [Job processing design](./job-processing.md) for the processing
architecture, job execution flow, Slicer CLI registration guidance, and the
category scoping that keeps VolView tasks separate from HistomicsUI and
DIVE-DSA tasks in a shared `slicer_cli_web` deployment.

### Radiology CLI task image

The development stack uses the
[VolView Radiology CLI](https://github.com/PaulHax/volview-radiology-cli) as
reference infrastructure to drive and test the processing backend. Clone it
locally and set `CLI_REPO` in this repository's `.env` to that checkout:

```sh
git clone https://github.com/PaulHax/volview-radiology-cli
# In girder_volview/.env:
CLI_REPO=/path/to/volview-radiology-cli
```

When processing routes are present, `script/deploy` calls
`script/ensure-radiology-cli`. That script builds the local
`volview-radiology-cli:latest` image if it is missing, registers it with
`slicer_cli_web`, and verifies the declared tasks are available. It does not
pull this image from a registry.

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
