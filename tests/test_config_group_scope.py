"""Server-fixture coverage for group-scoped ``.volview_config.yaml`` merging.

``yamlConfigFile`` only calls ``adjustConfigForUser`` when the top-level config
carries an ``access`` or ``groups`` key. The gate must recognize the plural
``groups`` key (the one ``adjustConfigForUser`` actually merges), and the raw
``groups`` mapping must never leak to the client -- for members it merges, for
non-members it is simply stripped.

Needs a live pytest-girder Mongo; self-skips when unreachable.
"""

import io

from conftest import mongo_reachable

import pytest


pytestmark = pytest.mark.skipif(
    not mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_item_launch_parity); "
    "unavailable offline",
)


CONFIG_PATH = "/folder/%s/volview_config/.volview_config.yaml"

_GROUPS_ONLY_YAML = (
    b"defaultLayout: axial\ngroups:\n  readers:\n    defaultLayout: grid\n"
)


# The shared ``owner`` fixture lives in conftest.


@pytest.fixture
def configFolder(fsAssetstore, owner):
    from girder.models.folder import Folder
    from girder.models.upload import Upload

    folder = Folder().createFolder(
        owner, "study", parentType="user", creator=owner, public=False
    )
    Upload().uploadFromFile(
        io.BytesIO(_GROUPS_ONLY_YAML),
        size=len(_GROUPS_ONLY_YAML),
        name=".volview_config.yaml",
        parentType="folder",
        parent=folder,
        user=owner,
    )
    return folder


def _get_config(server, folder, user):
    resp = server.request(
        path=CONFIG_PATH % folder["_id"],
        method="GET",
        user=user,
        isJson=True,
        exception=True,
    )
    assert resp.output_status.startswith(b"200")
    return resp.json


@pytest.mark.plugin("volview")
def test_groups_only_config_merges_for_group_member(server, owner, configFolder):
    from girder.models.group import Group
    from girder.models.user import User

    group = Group().createGroup("readers", creator=owner)
    Group().addUser(group, owner)
    member = User().load(owner["_id"], force=True)

    config = _get_config(server, configFolder, member)
    # The member's group section overrides the base value...
    assert config["defaultLayout"] == "grid"
    # ...and the raw groups mapping never leaks to the client.
    assert "groups" not in config


@pytest.mark.plugin("volview")
def test_groups_only_config_is_stripped_for_non_member(server, owner, configFolder):
    config = _get_config(server, configFolder, owner)
    # No membership -> base value stands, and the groups key is still stripped.
    assert config["defaultLayout"] == "axial"
    assert "groups" not in config
