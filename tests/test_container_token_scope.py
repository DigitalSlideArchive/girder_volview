"""Offline unit coverage for the CLI-container token.

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
from girder_volview.backend import outputs, routes


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
            captured["createToken"] = {"user": user, "scope": scope, **kwargs}
            return {"_id": "fake-token", "scope": scope}

    class _Handler:
        def subHandler(self, cliItem, params, user, token):
            captured["token"] = token
            captured["params"] = params
            captured["cliItem"] = cliItem
            return {"_id": "job-1"}

    slicerPkg = types.ModuleType("slicer_cli_web")
    slicerMod = types.ModuleType("slicer_cli_web.rest_slicer_cli")
    slicerMod.genHandlerToRunDockerCLI = lambda cliItem: _Handler()
    slicerPkg.rest_slicer_cli = slicerMod

    monkeypatch.setitem(sys.modules, "slicer_cli_web", slicerPkg)
    monkeypatch.setitem(sys.modules, "slicer_cli_web.rest_slicer_cli", slicerMod)
    monkeypatch.setattr("girder.models.token.Token", _FakeToken)
    return captured


def test_container_token_is_scope_limited_to_data_read_write(fakeDockerStack):
    cliItem = types.SimpleNamespace(
        name="Median", xml="<executable/>", item={"meta": {}}
    )
    user = {"_id": "user-1", "login": "u"}
    # Representative initial job fields. The container token is NEVER persisted
    # on the job, so `_genDockerJob` adds no key to what we passed.
    fields = {outputs._OUTPUT_FOLDER_ID_FIELD: "an-output-folder-id"}

    job = routes._genDockerJob(cliItem, {"inputVolume": "abc"}, user, fields)

    # (1) Minted scope-limited to exactly the data plane a CLI needs.
    assert fakeDockerStack["createToken"]["scope"] == _EXPECTED_SCOPE
    # ...for the submitting user (their own ACLs still bound it).
    assert fakeDockerStack["createToken"]["user"] is user
    # ...with an explicit short TTL, so the credential cannot outlive the job by
    # months (Girder would otherwise apply the 180-day cookie_lifetime default).
    assert fakeDockerStack["createToken"]["days"] == routes._CONTAINER_TOKEN_TTL_DAYS
    # (2) That same scoped token is what flows to the CLI handler.
    assert fakeDockerStack["token"]["scope"] == _EXPECTED_SCOPE
    # (3) The initial fields are injected verbatim into the CLI's job otherFields,
    # and nothing extra (no minted token) is smuggled in alongside them.
    injected = fakeDockerStack["cliItem"].item["meta"]["docker-params"][
        "girder_job_other_fields"
    ]
    assert injected == fields
    assert job == {"_id": "job-1"}
