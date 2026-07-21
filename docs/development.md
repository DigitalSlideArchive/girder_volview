# Development

## Dev stack

Get a stack running with
https://github.com/DigitalSlideArchive/digital_slide_archive/tree/master/devops/with-dive-volview

In the `docker-compose.override.yml` file, add a volume mounting your checkout
of this plugin (replace `/path/to/girder_volview` with wherever you cloned it —
the paths make no assumption about checkouts sitting next to each other):

```yaml
services:
  girder:
    volumes:
      - ../with-dive-volview/provision.divevolview.yaml:/opt/digital_slide_archive/devops/dsa/provision.yaml
      - /path/to/girder_volview:/opt/girder_volview
```

Comment out the pip install of this plugin here: https://github.com/DigitalSlideArchive/digital_slide_archive/blob/master/devops/with-dive-volview/provision.divevolview.yaml#L3

To install the volume-mapped plugin and incorporate changes as files are
edited, add this to the `shell` section of the provision.yaml:

```yaml
shell:
  - cd /opt/girder_volview/ && pip install -e .
  - (sleep 30 && girder build --dev --watch-plugin volview)&
```

## API endpoints

- GET folder/:id/volview?items=[itemIds]&folders=[folderIds] -> download JSON with URLS to files or the latest `*.volview.zip` file in the folder
- GET item/:id/volview -> download JSON with URLs to all files in item or the latest `*.volview.zip` file
- POST item/:id/volview -> upload file to Item with cookie authentication
- GET file/:id/proxiable/:name -> download a file with option to proxy
- GET folder/:id/volview_config/:name -> download JSON with VolView config properties

The launch-manifest routes' resume/fresh semantics are documented in
[sessions.md](./sessions.md).

## Develop the VolView client

The VolView client is consumed as the `volview` npm package: `girder build`
installs the version pinned in `girder_volview/web_client/package.json` and
serves its `dist/`; the backend conformance tests read the same package's
`backend-contract/`. To develop against an unreleased VolView build, make
`girder_volview/web_client/node_modules/volview` BE your local build instead
of the pinned release — either `npm link` your VolView checkout, or
`npm pack` it and install the tarball (the closer match to what a release
does, since it goes through the package's `files` allowlist):

```sh
cd /path/to/VolView && npm run build && npm pack
npm --prefix girder_volview/web_client install /path/to/VolView/volview-*.tgz
```

Then rebuild/restart girder so the served dist is refreshed. Mounting or
copying only a `dist/` over `node_modules/volview/dist` also works for
UI-only iteration, but leaves the package's `backend-contract` at the pinned
version — fine for the browser, wrong for the conformance tests.

Processing (the Jobs tab) and remote session save ship in every build
and no longer need build-time env flags — `VITE_ENABLE_PROCESSING`,
`VITE_ENABLE_REMOTE_SAVE`, and `VITE_PROCESSING_ALLOWED_ORIGINS` were removed.
What the deployed client is allowed to contact is decided at runtime by a
same-origin egress gate; a same-origin deployment (such as DSA) needs no
configuration, and cross-origin targets are never allowed.

See [Job processing design](./job-processing.md) for the execution flow and how
Girder VolView integrates with Slicer CLI Web. The client-facing recipe is in
[Building and deploying custom Slicer CLIs](./custom-slicer-clis.md). Local
reference-image registration is in
[Server administration](./admin.md#local-reference-image).

## Updating the VolView client version

1. Update the [volview](https://www.npmjs.com/package/volview?activeTab=versions) version in `./girder_volview/web_client/package.json`

## Backend contract and tests

The backend's conformance tests validate the server against VolView's
`backend-contract` — the ONE normative copy of the wire fixtures + generated
JSON Schemas. This repo keeps **no vendored copy**; the tests read the contract
from wherever the `volview` dependency is installed
(`girder_volview/web_client/node_modules/volview/backend-contract`, shipped in
the package's `files`).

- **Against the pinned release** — fetch the pinned `volview`, then run the
  suite:

  ```sh
  npm --prefix girder_volview/web_client install
  tox -e test        # or: pytest
  ```

- **Against an unreleased VolView branch** (developing the two together) — link
  a local VolView checkout so the tests read that branch's contract:

  ```sh
  cd <VolView checkout> && npm link
  npm --prefix girder_volview/web_client link volview
  ```

  Or point the tests straight at a checkout, no link required:

  ```sh
  GIRDER_VOLVIEW_CONTRACT_DIR=<VolView checkout>/backend-contract pytest
  ```

CI installs the **pinned published** `volview`, so when the backend is developed
ahead of the latest published contract the conformance tests are expected to be
red; they go green once VolView publishes (a merge-to-main dev release) and the
`volview` pin here is bumped to it.

## Browser e2e harness

`e2e/` has one Playwright harness. `npm test` exports and deploys the pinned
baseline, captures sessions through its real UI, redeploys this worktree,
verifies backwards compatibility, and then runs fresh current-version
save/load/restore and job scenarios. `npm run compat` is an alias for the same
coverage-first run. No second backend checkout or committed session fixture is
required. See [compat-e2e.md](compat-e2e.md).

Both deploy through `script/deploy`, which reads machine-specific paths from a
gitignored repo-root `.env` (copy `.env.example`).

Sample-data tooling (including the harness's cached DICOM tier) lives in
[`e2e/seed/`](../e2e/seed/README.md).
