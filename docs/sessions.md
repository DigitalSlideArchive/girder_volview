# Save / restore round-trip

Every launch URL carries query params the client acts on:

- `urls=` — where the client fetches the scene to load. Each gesture has one
  meaning: a **raw pick** (an item or checked images) always loads fresh; a
  **checked session item** opens exactly that saved session (back-in-history);
  a **filter gesture** (grouped DICOM row) resumes its newest matching session,
  else the filtered images fresh; a **bare folder open** resumes the folder's
  newest `session.volview.zip`, or the folder's raw images if none has been
  saved yet.
- `save=` — the ordinary session-zip save route: item-scoped
  (`POST item/:id/volview`) for a single item, or folder-scoped
  (`POST folder/:id/volview?metadata=…`) for a checked or filter set, where
  `metadata` records that set under the saved session's `linkedResources`.
- `config=` — the folder's VolView config (`GET folder/:id/volview_config/:name`).

On Save, the plugin writes a `session.volview.zip` and returns a **`resumeUrl`**
(`item/:id/volview`, pointing at the session item). The client repoints ONLY its
`urls=` at that `resumeUrl` — `save=` stays as launched — so:

- a browser refresh (F5) reloads the just-made save directly from its item, and
- repeated folder-scoped saves each mint a **new** `session.volview.zip` item in
  the folder; F5 and a bare folder re-open track the newest.

## Open Item

1. User clicks Open in VolView for an item. If the item has no `session.volview.zip`, VolView opens on the item's raw files; if it has one, VolView opens on the newest `session.volview.zip`.
1. User clicks Save. VolView POSTs `session.volview.zip` to `item/:id/volview`; the plugin stores it in the item and returns the `resumeUrl`.
1. Refresh, or re-open, resumes that saved session from the same item.

## Open Checked

1. User checks a set of items/folders and clicks "Open Checked in VolView". VolView opens fresh on exactly the checked set (`GET folder/:id/volview?items=[…]&folders=[…]`) — checking raw images ALWAYS opens fresh, even when a newer matching save exists. Checking a `session.volview.zip` item instead opens exactly that saved session (back-in-history).
1. User clicks Save. A `session.volview.zip` is created in the folder with the checked set recorded under `linkedResources`; the plugin returns its `resumeUrl`, which the client repoints `urls=` at.
1. Refresh reloads that saved session; each subsequent save mints a new session item in the folder. To get back to a save later, open the folder bare (newest save) or check the session item itself (that save).

## Open Filter-Linked Session (Grouped DICOM Row)

Filter-linked sessions record a `linkedResources.filter` (a metadata key/value dict like `{"meta.dicom.StudyInstanceUID": "..."}`) in place of explicit item/folder IDs. The grouped DICOM row opener produces these.

1. User clicks Open on a grouped row. If a session with a matching filter exists, VolView resumes the newest one; otherwise it opens fresh on the raw DICOM files matching the filter (`GET folder/:id/volview?filters={…}`).
1. User clicks Save. A `session.volview.zip` is created in the folder with the row's filter recorded under `linkedResources.filter`; the client repoints `urls=` at its `resumeUrl`.
1. Refresh reloads the saved session; each subsequent save mints a new session item in the folder.
