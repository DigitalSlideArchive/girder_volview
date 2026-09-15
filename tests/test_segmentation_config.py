"""Filename association configuration at the generated-config boundary."""

import copy

import pytest
from girder.exceptions import RestException

from girder_volview.backend.launch import BASE_CONFIG, _mergeDictionaries


@pytest.mark.parametrize("key", ["segmentationExtension", "segmentGroupExtension"])
@pytest.mark.parametrize("extension", ["mask", ""])
def test_override_normalizes_before_defaults_merge(key, extension):
    config = copy.deepcopy(BASE_CONFIG)
    override = {"io": {key: extension}}
    original = copy.deepcopy(override)
    _mergeDictionaries(config, override)
    assert config["io"] == {
        "segmentationExtension": extension,
        "segmentGroupSaveFormat": "nii.gz",
        "layerExtension": "layer",
    }
    assert override == original


@pytest.mark.parametrize("extension", ["seg", ""])
def test_matching_aliases_are_canonical(extension):
    config = {}
    _mergeDictionaries(
        config,
        {
            "io": {
                "segmentationExtension": extension,
                "segmentGroupExtension": extension,
            }
        },
    )
    assert config == {"io": {"segmentationExtension": extension}}


@pytest.mark.parametrize("old,new", [("mask", "seg"), ("", "seg"), ("seg", "")])
def test_conflicting_aliases_are_rejected(old, new):
    with pytest.raises(RestException, match="conflicts with io.segmentationExtension"):
        _mergeDictionaries(
            {},
            {
                "io": {
                    "segmentationExtension": new,
                    "segmentGroupExtension": old,
                }
            },
        )


def test_inherited_legacy_setting_can_be_overridden_by_new_name():
    config = {"io": {"segmentGroupExtension": "mask"}}
    _mergeDictionaries(config, {"io": {"segmentationExtension": "seg"}})
    assert config == {"io": {"segmentationExtension": "seg"}}


def test_io_replacement_retains_all_semantics():
    config = copy.deepcopy(BASE_CONFIG)
    _mergeDictionaries(config, {"io": {"__all__": True, "segmentGroupExtension": ""}})
    assert config["io"] == {"segmentationExtension": ""}
