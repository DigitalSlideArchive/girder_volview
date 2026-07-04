"""Offline unit coverage for Chunk 21 item (c): the CLI-container token.

``_genDockerJob`` mints the token that ``slicer_cli_web`` injects into the CLI
container. This asserts it is minted **scope-limited** to ``DATA_READ`` +
``DATA_WRITE`` (narrower than the ecosystem norm, where ``slicer_cli_web`` mints a
full-auth token) and that this *same* scoped token is what reaches the CLI handler.

Pure unit test: ``slicer_cli_web`` (not installed in the offline gate) and the
Girder ``Token`` model are faked, so it needs no Mongo and always runs.
"""

import sys
import types

import pytest

from girder.constants import TokenScope
from girder_volview.facade import processing


_EXPECTED_SCOPE = [TokenScope.DATA_READ, TokenScope.DATA_WRITE]


@pytest.fixture
def fakeDockerStack(monkeypatch):
    """Fake the two ``_genDockerJob`` touch points so it runs without docker/Mongo.

    - ``girder.models.token.Token`` -> a stub whose ``createToken`` records the
      ``scope`` it was minted with and echoes it onto the returned token.
    - ``slicer_cli_web.rest_slicer_cli.genHandlerToRunDockerCLI`` -> a handler
      whose ``subHandler`` records the token it was handed.
    """
    captured = {}

    class _FakeToken:
        def createToken(self, user=None, scope=None, **kwargs):
            captured["createToken"] = {"user": user, "scope": scope}
            return {"_id": "fake-token", "scope": scope}

        def find(self, query=None, **kwargs):
            # _genDockerJob captures the per-hook upload tokens minted during
            # subHandler; none exist in this faked stack.
            captured["find"] = query
            return []

    class _Handler:
        def subHandler(self, cliItem, params, user, token):
            captured["token"] = token
            captured["params"] = params
            return {"_id": "job-1"}

    slicerPkg = types.ModuleType("slicer_cli_web")
    slicerMod = types.ModuleType("slicer_cli_web.rest_slicer_cli")
    slicerMod.genHandlerToRunDockerCLI = lambda cliItem: _Handler()
    slicerPkg.rest_slicer_cli = slicerMod

    monkeypatch.setitem(sys.modules, "slicer_cli_web", slicerPkg)
    monkeypatch.setitem(sys.modules, "slicer_cli_web.rest_slicer_cli", slicerMod)
    monkeypatch.setattr("girder.models.token.Token", _FakeToken)
    # Reference-bound output recording needs a real job/db; not under test here.
    monkeypatch.setattr(
        processing, "_bindJobOutputs", lambda job, token, xml, uploadTokens=None: None
    )
    return captured


def test_container_token_is_scope_limited_to_data_read_write(fakeDockerStack):
    cliItem = types.SimpleNamespace(name="Median", xml="<executable/>")
    user = {"_id": "user-1", "login": "u"}

    job = processing._genDockerJob(cliItem, {"inputVolume": "abc"}, user)

    # (1) Minted scope-limited to exactly the data plane a CLI needs.
    assert fakeDockerStack["createToken"]["scope"] == _EXPECTED_SCOPE
    # ...for the submitting user (their own ACLs still bound it).
    assert fakeDockerStack["createToken"]["user"] is user
    # (2) That same scoped token is what flows to the CLI handler.
    assert fakeDockerStack["token"]["scope"] == _EXPECTED_SCOPE
    assert job == {"_id": "job-1"}
