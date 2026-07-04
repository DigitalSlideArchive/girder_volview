"""Server-fixture coverage for the facade CSRF defense (WORKORDER chunk 3).

Two things the offline ``test_csrf`` unit tests cannot show, exercised here
through the real cherrypy pipeline:

1. *Behavior on the wire* -- a genuine cross-site / no-signal POST to a write
   route is rejected 403 before it touches the model layer, while a same-origin
   POST passes the guard and proceeds (here to a 400 "invalid folder id", since
   the guard is the only difference between the two requests). Because the
   pytest-girder harness always sends ``Host: 127.0.0.1``, a same-origin pass
   with ``Origin: https://volview.example`` only succeeds if the deployment
   origin is derived from ``X-Forwarded-Host`` -- so these tests also pin the
   forwarded-header derivation. Requests here authenticate with a Girder-Token
   header, so a same-origin pass + a cross-site block on the *same* auth mode
   also show the check is applied uniformly, not branched on auth mode.

2. *Coverage* -- every state-changing route the plugin registers carries the
   guard, enumerated from the live routing table so a new write route added
   without ``@csrfProtect`` fails this test.

Like ``test_load`` this needs a live pytest-girder server + Mongo; the module
self-skips when the test Mongo is unreachable so the offline gate stays green,
and runs (and must pass) wherever Mongo is present.
"""

import os
import socket

import cherrypy
import pytest

from pytest_girder.utils import getResponseBody

from girder_volview.csrf import CSRF_PROTECTED_ATTR, REJECT_MESSAGE


DEPLOYMENT = "volview.example"

# runTask: reject (cross-site) lands in the CSRF guard before the model load; a
# same-origin request reaches the modelParam and 400s on this bogus folder id.
RUN_PATH = "/folder/notanid/volview_processing/tasks/sometask/run"


# ---------------------------------------------------------------------------
# Self-skip when no live test Mongo is reachable (mirrors test_load)
# ---------------------------------------------------------------------------

def _mongo_reachable(timeout=0.5):
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
    reason="needs a live pytest-girder Mongo (like test_load); unavailable offline",
)


@pytest.fixture
def user(db):
    from girder.models.user import User
    return User().createUser(
        login="csrfadmin",
        password="password123",
        firstName="Csrf",
        lastName="Admin",
        email="csrf@example.com",
        admin=True,
    )


def _proxyHeaders(*extra):
    """Reverse-proxy view (host/proto) plus any request-specific headers."""
    return [
        ("X-Forwarded-Host", DEPLOYMENT),
        ("X-Forwarded-Proto", "https"),
    ] + list(extra)


def _post(server, user=None, headers=()):
    return server.request(
        path=RUN_PATH,
        method="POST",
        user=user,
        additionalHeaders=_proxyHeaders(*headers),
        isJson=False,
        exception=True,
    )


# ---------------------------------------------------------------------------
# Behavior on the wire -- blocked vs. passed the guard
# ---------------------------------------------------------------------------

@pytest.mark.plugin("volview")
def test_same_origin_write_passes_guard(server, user):
    resp = _post(
        server, user,
        [("Origin", "https://" + DEPLOYMENT), ("Sec-Fetch-Site", "same-origin")],
    )
    # Past the guard: it reached the folder modelParam and 400'd on the bogus id.
    assert resp.output_status.startswith(b"400")
    assert REJECT_MESSAGE not in getResponseBody(resp)


@pytest.mark.plugin("volview")
def test_same_origin_via_sec_fetch_site_only_passes_guard(server, user):
    # No Origin header (Safari-style same-origin write); Sec-Fetch-Site vouches.
    resp = _post(server, user, [("Sec-Fetch-Site", "same-origin")])
    assert resp.output_status.startswith(b"400")


@pytest.mark.plugin("volview")
def test_cross_site_write_is_blocked(server, user):
    resp = _post(
        server, user,
        [("Origin", "https://evil.example"), ("Sec-Fetch-Site", "cross-site")],
    )
    assert resp.output_status.startswith(b"403")
    assert REJECT_MESSAGE in getResponseBody(resp)


@pytest.mark.plugin("volview")
def test_foreign_origin_write_is_blocked(server, user):
    # Sec-Fetch-Site stripped by an intermediary; the foreign Origin still loses.
    resp = _post(server, user, [("Origin", "https://evil.example")])
    assert resp.output_status.startswith(b"403")
    assert REJECT_MESSAGE in getResponseBody(resp)


@pytest.mark.plugin("volview")
def test_missing_browser_headers_fails_closed(server, user):
    # Neither Origin nor Sec-Fetch-Site: no browser signal at all -> rejected.
    resp = _post(server, user)
    assert resp.output_status.startswith(b"403")
    assert REJECT_MESSAGE in getResponseBody(resp)


@pytest.mark.plugin("volview")
def test_cross_site_blocked_without_authentication(server):
    # The guard does not depend on auth mode: an unauthenticated cross-site
    # write is rejected by the same 403.
    resp = _post(server, None, [("Sec-Fetch-Site", "cross-site")])
    assert resp.output_status.startswith(b"403")
    assert REJECT_MESSAGE in getResponseBody(resp)


# ---------------------------------------------------------------------------
# Coverage -- every plugin write route is guarded (enumerated from the router)
# ---------------------------------------------------------------------------

def _pluginWriteRoutes():
    """(method, route, handler) for every state-changing route this plugin
    registered, discovered from the live cherrypy routing table.

    Scans every REST resource under the API root, not just folder/item, so a
    future write route on any resource is covered too.
    """
    apiRoot = cherrypy.tree.apps[""].root.api.v1
    writeMethods = ("post", "put", "patch", "delete")
    found = []
    for attr in dir(apiRoot):
        resource = getattr(apiRoot, attr, None)
        routes = getattr(resource, "_routes", None)
        if not isinstance(routes, dict):
            continue
        for method in writeMethods:
            for routesOfLen in routes.get(method, {}).values():
                for route, handler in routesOfLen:
                    module = getattr(handler, "__module__", "") or ""
                    if module.startswith("girder_volview"):
                        found.append((method, route, handler))
    return found


@pytest.mark.plugin("volview")
def test_all_plugin_write_routes_are_csrf_protected(server):
    found = _pluginWriteRoutes()

    # Guard against a vacuous pass: the three known write routes must be present,
    # so a broken introspection (empty result) fails rather than passes.
    names = sorted(handler.__name__ for _, _, handler in found)
    assert names == ["runTask", "saveToFolder", "saveToItem"], names

    unguarded = [
        (method, route, handler.__name__)
        for method, route, handler in found
        if not getattr(handler, CSRF_PROTECTED_ATTR, False)
    ]
    assert unguarded == [], "write routes missing @csrfProtect: %r" % (unguarded,)
