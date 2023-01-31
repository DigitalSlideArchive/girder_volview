import pytest

from girder.plugin import loadedPlugins


@pytest.mark.plugin('volview')
def test_import(server):
    assert 'volview' in loadedPlugins()
