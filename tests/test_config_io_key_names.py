"""Served-config coverage for VolView's renamed ``io`` keys.

The plugin's defaults are written under the current key names and merged under
a folder's ``.volview_config.yaml``. A folder config still written under the
earlier names must keep overriding those defaults, at every layer the config
route merges: the folder's own file, an inherited parent file, and a group
block.

Needs a live pytest-girder Mongo; self-skips when unreachable.
"""

import io

from conftest import mongo_reachable

import pytest


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo; unavailable offline",
)


CONFIG_PATH = "/folder/%s/volview_config/.volview_config.yaml"


def _folder_with_config(owner, yamlBytes, name="study", parent=None):
    from girder.models.folder import Folder
    from girder.models.upload import Upload

    folder = Folder().createFolder(
        parent or owner,
        name,
        parentType="folder" if parent else "user",
        creator=owner,
        public=False,
    )
    if yamlBytes is not None:
        Upload().uploadFromFile(
            io.BytesIO(yamlBytes),
            size=len(yamlBytes),
            name=".volview_config.yaml",
            parentType="folder",
            parent=folder,
            user=owner,
        )
    return folder


def _served_io(server, folder, user):
    resp = server.request(
        path=CONFIG_PATH % folder["_id"],
        method="GET",
        user=user,
        isJson=True,
        exception=True,
    )
    assert resp.output_status.startswith(b"200")
    return resp.json["io"]


@pytest.mark.plugin("volview")
def test_defaults_are_served_under_the_current_names(server, owner, fsAssetstore):
    folder = _folder_with_config(owner, None)
    assert _served_io(server, folder, owner) == {
        "segmentationExtension": "seg",
        "segmentationSaveFormat": "nii.gz",
        "layerExtension": "layer",
    }


@pytest.mark.plugin("volview")
def test_earlier_names_in_a_folder_config_override_the_defaults(
    server, owner, fsAssetstore
):
    folder = _folder_with_config(
        owner,
        b"io:\n  segmentGroupExtension: mask\n  segmentGroupSaveFormat: nrrd\n",
    )
    assert _served_io(server, folder, owner) == {
        "segmentationExtension": "mask",
        "segmentationSaveFormat": "nrrd",
        "layerExtension": "layer",
    }


@pytest.mark.plugin("volview")
def test_current_names_in_a_folder_config_override_the_defaults(
    server, owner, fsAssetstore
):
    folder = _folder_with_config(owner, b"io:\n  segmentationSaveFormat: nrrd\n")
    served = _served_io(server, folder, owner)
    assert served["segmentationSaveFormat"] == "nrrd"
    assert "segmentGroupSaveFormat" not in served


@pytest.mark.plugin("volview")
def test_child_current_name_overrides_inherited_earlier_name(
    server, owner, fsAssetstore
):
    parent = _folder_with_config(
        owner, b"io:\n  segmentGroupSaveFormat: nrrd\n", name="parent"
    )
    child = _folder_with_config(
        owner,
        b"__inherit__: true\nio:\n  segmentationSaveFormat: mha\n",
        name="child",
        parent=parent,
    )
    served = _served_io(server, child, owner)
    assert served["segmentationSaveFormat"] == "mha"
    assert "segmentGroupSaveFormat" not in served


@pytest.mark.plugin("volview")
def test_group_block_earlier_name_overrides_the_folder_current_name(
    server, owner, fsAssetstore
):
    from girder.models.group import Group
    from girder.models.user import User

    group = Group().createGroup("readers", creator=owner)
    Group().addUser(group, owner)
    member = User().load(owner["_id"], force=True)

    folder = _folder_with_config(
        owner,
        b"io:\n  segmentationSaveFormat: nrrd\n"
        b"groups:\n  readers:\n    io:\n      segmentGroupSaveFormat: mha\n",
    )
    served = _served_io(server, folder, member)
    assert served["segmentationSaveFormat"] == "mha"
    assert "segmentGroupSaveFormat" not in served


@pytest.mark.plugin("volview")
def test_one_block_naming_a_key_both_ways_reaches_the_client_intact(
    server, owner, fsAssetstore
):
    # The client prefers the current name and tells the config's author about
    # the ignored one; the server does not pick a winner on their behalf.
    folder = _folder_with_config(
        owner,
        b"io:\n  segmentGroupSaveFormat: nrrd\n  segmentationSaveFormat: mha\n",
    )
    served = _served_io(server, folder, owner)
    assert served["segmentGroupSaveFormat"] == "nrrd"
    assert served["segmentationSaveFormat"] == "mha"
