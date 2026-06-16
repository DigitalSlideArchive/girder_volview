"""Coverage for the additive result `intent` emitted by the processing facade.

`_collectJobResults` builds each result entry's `intent` from the parsed CLI
output via `_intentForOutput`, mirroring the legacy `role` logic with the same
five-name vocabulary the VolView client validates (see
`src/processing/intents.ts`). The full collector needs a live slicer_cli_web +
Girder models, so the manifest-shape coverage here exercises the pure intent
mapping that decides each entry's intent.
"""

from girder_volview.facade.processing import _intentForOutput


def _out(tag, isLabel):
    return {"name": "out", "tag": tag, "isLabel": isLabel, "fileExtensions": ""}


def test_labelmap_image_maps_to_attach_segment_group():
    # Mirrors role == "segmentGroup".
    assert _intentForOutput(_out("image", True)) == "attach-segment-group"


def test_plain_image_maps_to_add_base_image():
    assert _intentForOutput(_out("image", False)) == "add-base-image"


def test_non_image_file_maps_to_download():
    assert _intentForOutput(_out("file", False)) == "download"


def test_labelmap_wins_over_non_image_tag():
    # `isLabel` is checked before `tag`, so a labelmap file is still a
    # segment group rather than a download.
    assert _intentForOutput(_out("file", True)) == "attach-segment-group"
