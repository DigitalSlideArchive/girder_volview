"""Server-fixture coverage for the getTaskXml HTTP 404 contract (item 4.7).

The 500->404 fix (``ba41ced``) for a filtered-out / unknown / slicer_cli_web-
missing task id is an HTTP-pipeline interaction the offline helper tests in
``test_task_scoping`` cannot catch: ``getTaskXml`` armed ``setRawResponse()``
*before* the scope guards, so a raised ``RestException``'s str body hit
cherrypy's ``collapse_body`` in raw-response mode ("expected a bytes-like
object, str found") and surfaced as a 500 instead of the intended 404 on every
non-happy path. The offline tests drive ``_findScopedCliItem`` directly and so
never exercise the raw-response arming; only the live ``EXPECT_SCOPED_TASKS=1``
smoke guarded the wire status. This puts the contract under pytest by routing a
real request through the cherrypy pipeline and asserting the status line.

Only the slicer_cli_web boundary is stubbed (``_slicerCliAvailable`` /
``_findCliItem``): the real ``_findScopedCliItem`` + ``<category>`` scope filter
and the real handler/cherrypy pipeline run, so the test needs neither
slicer_cli_web nor any registered docker CLI.

Like ``test_load.py::test_import`` this needs a live pytest-girder server +
Mongo, which ``.venv-ralph`` (the per-item GATE-FACADE venv) does not provide.
Rather than a name-based ``-k`` exclusion, the module self-skips when the test
Mongo is unreachable, so the offline gate stays green without changing its
selector and the test still runs (and must pass) wherever a real test Mongo is
present.
"""

import os
import socket
import types

import pytest

from girder_volview.facade import processing

from pytest_girder.utils import getResponseBody


# ---------------------------------------------------------------------------
# Self-skip when no live test Mongo is reachable (mirrors test_import's need)
# ---------------------------------------------------------------------------

def _mongo_reachable(timeout=0.5):
    """True if a quick TCP connect to the pytest-girder test Mongo succeeds.

    Honors ``GIRDER_TEST_DB`` (``mongodb://host:port/db``) when set, else the
    pytest-girder default of ``localhost:27017``. A refused connection returns
    fast, so collection is not slowed when Mongo is down.

    Caveat: pytest-girder's ``--mongo-uri`` option is a pytest CLI argument not
    available when this module-level ``skipif`` is evaluated, so a non-default
    Mongo selected only via ``--mongo-uri`` (without a matching
    ``GIRDER_TEST_DB``) is not detected — the worst case is a false skip (lost
    coverage), never a false pass. Export ``GIRDER_TEST_DB`` to run there.
    """
    host, port = "localhost", 27017
    uri = os.environ.get("GIRDER_TEST_DB", "")
    if uri.startswith("mongodb://"):
        netloc = uri[len("mongodb://"):].split("/", 1)[0].split(",", 1)[0]
        if ":" in netloc:
            host, port_str = netloc.rsplit(":", 1)
            port = int(port_str) if port_str.isdigit() else port
        elif netloc:
            host = netloc
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _mongo_reachable(),
    reason="needs a live pytest-girder Mongo (like test_import); unavailable in .venv-ralph",
)


# ---------------------------------------------------------------------------
# Fakes — a radiology + a pathology CLI carrying just the .xml/.name scoping reads
# ---------------------------------------------------------------------------

def _xml(category, name):
    """A minimal Slicer Execution Model XML with a ``<category>``."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<executable>\n"
        f"  <category>{category}</category>\n"
        f"  <title>{name}</title>\n"
        "  <description>x</description>\n"
        "</executable>\n"
    )


_RADIOLOGY_XML = _xml("Radiology", "MedianFilter")


@pytest.fixture
def stub_slicer(monkeypatch):
    """Stub the slicer_cli_web boundary with a fixed radiology/pathology catalog.

    The real ``_findScopedCliItem`` + scope filter still run; only the optional
    dependency's availability + lookup are replaced, so the test is independent
    of whether slicer_cli_web is installed in the running environment.
    """
    catalog = {
        "radid": types.SimpleNamespace(name="MedianFilter", xml=_RADIOLOGY_XML),
        "pathid": types.SimpleNamespace(
            name="NucleiDetection", xml=_xml("HistomicsTK", "NucleiDetection")
        ),
    }
    monkeypatch.delenv(processing._ALLOWED_CATEGORIES_ENV, raising=False)
    monkeypatch.setattr(processing, "_slicerCliAvailable", lambda: True)
    monkeypatch.setattr(
        processing, "_findCliItem", lambda taskId, user: catalog.get(taskId)
    )
    return catalog


@pytest.fixture
def user(db):
    from girder.models.user import User
    return User().createUser(
        login="radadmin",
        password="password123",
        firstName="Rad",
        lastName="Admin",
        email="rad@example.com",
        admin=True,
    )


@pytest.fixture
def folder(db, user):
    from girder.models.folder import Folder
    return Folder().createFolder(
        parent=user, parentType="user", name="vol", creator=user
    )


def _xml_path(folder, taskId):
    return f"/folder/{folder['_id']}/volview_processing/tasks/{taskId}/xml"


# ---------------------------------------------------------------------------
# Happy path: a radiology id returns 200 + application/xml + the CLI's XML
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_radiology_task_xml_returns_200_xml(server, user, folder, stub_slicer):
    resp = server.request(
        path=_xml_path(folder, "radid"), method="GET", user=user, isJson=False
    )
    assert resp.output_status.startswith(b"200")
    assert "application/xml" in resp.headers.get("Content-Type", "")
    assert getResponseBody(resp) == _RADIOLOGY_XML


# ---------------------------------------------------------------------------
# The regression: a non-happy path must be 404, not the pre-ba41ced 500
# (exception=True so a regressed 500 surfaces as a clear "500 != 404", not the
# request helper's generic auto-raise).
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_pathology_task_xml_returns_404_not_500(server, user, folder, stub_slicer):
    resp = server.request(
        path=_xml_path(folder, "pathid"),
        method="GET",
        user=user,
        isJson=False,
        exception=True,
    )
    assert resp.output_status.startswith(b"404")


@pytest.mark.plugin("volview")
def test_unknown_task_xml_returns_404_not_500(server, user, folder, stub_slicer):
    resp = server.request(
        path=_xml_path(folder, "bogus"),
        method="GET",
        user=user,
        isJson=False,
        exception=True,
    )
    assert resp.output_status.startswith(b"404")
