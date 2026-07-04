"""Unit coverage for the facade CSRF defense (WORKORDER chunk 3, D9 addendum).

These tests exercise the pure decision helpers and the decorator wiring in
``girder_volview.csrf`` directly, so they need neither a live Girder server nor
Mongo. The end-to-end HTTP behavior (a real cross-site request through the
cherrypy pipeline) and the "every write route is wrapped" enumeration live in
``test_csrf_routes`` behind the server fixture.

The rule under test (fail closed): reject when ``Sec-Fetch-Site`` is
``cross-site``, or when ``Origin`` is present and is not the deployment origin,
or when the request carries neither header; and derive the deployment origin
from ``X-Forwarded-Host`` / ``X-Forwarded-Proto`` (the reverse-proxy view),
never a local address.
"""

import types

import cherrypy
import pytest

from girder.exceptions import RestException

from girder_volview import csrf


DEPLOYMENT = "volview.example"


def _sameOriginHeaders(**overrides):
    """Headers a legitimate same-origin browser write carries behind the proxy."""
    headers = {
        "X-Forwarded-Host": DEPLOYMENT,
        "X-Forwarded-Proto": "https",
        "Origin": "https://" + DEPLOYMENT,
        "Sec-Fetch-Site": "same-origin",
    }
    headers.update(overrides)
    return headers


# ---------------------------------------------------------------------------
# normalizeOrigin -- default-port equivalence + fail-closed parsing
# ---------------------------------------------------------------------------

def test_normalize_origin_makes_default_port_explicit():
    assert csrf.normalizeOrigin("https://a") == ("https", "a", "443")
    assert csrf.normalizeOrigin("https://a:443") == ("https", "a", "443")
    assert csrf.normalizeOrigin("http://a") == ("http", "a", "80")
    # ...so a proxy that appends the default port compares equal to a bare host.
    assert csrf.normalizeOrigin("https://a") == csrf.normalizeOrigin("https://a:443")


def test_normalize_origin_is_case_insensitive_on_scheme_and_host():
    assert csrf.normalizeOrigin("HTTPS://VolView.Example") == ("https", "volview.example", "443")


def test_normalize_origin_keeps_explicit_nondefault_port():
    assert csrf.normalizeOrigin("https://a:8443") == ("https", "a", "8443")
    assert csrf.normalizeOrigin("https://a:8443") != csrf.normalizeOrigin("https://a")


def test_normalize_origin_rejects_unusable_values():
    # No scheme, no host, opaque origin, and an unparseable port all fail closed.
    assert csrf.normalizeOrigin("") is None
    assert csrf.normalizeOrigin(None) is None
    assert csrf.normalizeOrigin("volview.example") is None   # bare host, no scheme
    assert csrf.normalizeOrigin("null") is None              # sandboxed/opaque origin
    assert csrf.normalizeOrigin("https://a:notaport") is None


# ---------------------------------------------------------------------------
# deploymentOrigin -- forwarded headers win; never a local address
# ---------------------------------------------------------------------------

def test_deployment_origin_from_forwarded_headers():
    headers = {"X-Forwarded-Host": DEPLOYMENT, "X-Forwarded-Proto": "https"}
    assert csrf.deploymentOrigin(headers) == ("https", "volview.example", "443")


def test_deployment_origin_prefers_forwarded_host_over_request_host():
    # The reverse-proxy value is the browser-facing host; the Host cherrypy sees
    # may be an internal upstream. Forwarded must win.
    headers = {
        "X-Forwarded-Host": DEPLOYMENT,
        "X-Forwarded-Proto": "https",
        "Host": "internal-upstream:8080",
    }
    assert csrf.deploymentOrigin(headers) == ("https", "volview.example", "443")


def test_deployment_origin_takes_first_forwarded_hop():
    headers = {
        "X-Forwarded-Host": "volview.example, inner-proxy, upstream",
        "X-Forwarded-Proto": "https, http",
    }
    assert csrf.deploymentOrigin(headers) == ("https", "volview.example", "443")


def test_deployment_origin_falls_back_to_host_header_then_scheme():
    # No X-Forwarded-Host: fall back to the Host request header (still browser
    # supplied, never the local socket) and the caller-supplied scheme.
    headers = {"Host": DEPLOYMENT}
    assert csrf.deploymentOrigin(headers, fallbackScheme="http") == ("http", "volview.example", "80")


def test_deployment_origin_none_when_no_host_available():
    assert csrf.deploymentOrigin({}) is None


# ---------------------------------------------------------------------------
# isSameOriginWrite -- the fail-closed decision
# ---------------------------------------------------------------------------

def test_same_origin_request_passes():
    assert csrf.isSameOriginWrite(_sameOriginHeaders()) is True


def test_same_origin_via_sec_fetch_site_only_passes():
    # Safari historically omits Origin on same-origin writes; Sec-Fetch-Site
    # alone must still admit the request.
    headers = _sameOriginHeaders()
    del headers["Origin"]
    assert csrf.isSameOriginWrite(headers) is True


def test_same_origin_via_origin_only_passes():
    # An intermediary stripped Sec-Fetch-Site, but the matching Origin vouches.
    headers = _sameOriginHeaders()
    del headers["Sec-Fetch-Site"]
    assert csrf.isSameOriginWrite(headers) is True


def test_default_port_origin_matches_bare_deployment_host():
    headers = _sameOriginHeaders(Origin="https://" + DEPLOYMENT + ":443")
    assert csrf.isSameOriginWrite(headers) is True


def test_cross_site_is_blocked():
    assert csrf.isSameOriginWrite(_sameOriginHeaders(**{"Sec-Fetch-Site": "cross-site"})) is False


def test_cross_site_blocked_even_with_matching_origin():
    # Sec-Fetch-Site is authoritative: cross-site loses regardless of Origin.
    headers = _sameOriginHeaders(**{"Sec-Fetch-Site": "cross-site"})
    assert csrf.isSameOriginWrite(headers) is False


def test_foreign_origin_is_blocked():
    headers = _sameOriginHeaders(Origin="https://evil.example")
    del headers["Sec-Fetch-Site"]
    assert csrf.isSameOriginWrite(headers) is False


def test_same_site_but_cross_origin_is_blocked_by_origin():
    # Sec-Fetch-Site "same-site" is not "cross-site", so only the Origin check
    # catches a sibling-subdomain caller.
    headers = _sameOriginHeaders(
        Origin="https://other.example", **{"Sec-Fetch-Site": "same-site"}
    )
    assert csrf.isSameOriginWrite(headers) is False


def test_opaque_null_origin_is_blocked():
    headers = _sameOriginHeaders(Origin="null")
    del headers["Sec-Fetch-Site"]
    assert csrf.isSameOriginWrite(headers) is False


def test_missing_both_headers_fails_closed():
    headers = {"X-Forwarded-Host": DEPLOYMENT, "X-Forwarded-Proto": "https"}
    assert csrf.isSameOriginWrite(headers) is False


def test_present_origin_but_undeterminable_deployment_fails_closed():
    # No way to establish our own origin, yet the request declares one: reject.
    headers = {"Origin": "https://" + DEPLOYMENT}
    assert csrf.isSameOriginWrite(headers) is False


# ---------------------------------------------------------------------------
# csrfProtect decorator -- marker + gate over the live request headers
# ---------------------------------------------------------------------------

def _fakeRequest(monkeypatch, headers):
    monkeypatch.setattr(
        cherrypy, "request", types.SimpleNamespace(headers=headers, scheme="https")
    )


def test_decorator_stamps_the_enumeration_marker():
    @csrf.csrfProtect
    def handler(*args, **kwargs):
        return "ok"

    assert getattr(handler, csrf.CSRF_PROTECTED_ATTR) is True


def test_decorator_allows_same_origin_and_forwards_args(monkeypatch):
    calls = []

    @csrf.csrfProtect
    def handler(*args, **kwargs):
        calls.append((args, kwargs))
        return "ok"

    _fakeRequest(monkeypatch, _sameOriginHeaders())
    assert handler(1, folder="f") == "ok"
    assert calls == [((1,), {"folder": "f"})]


def test_decorator_rejects_cross_site_before_calling_handler(monkeypatch):
    called = []

    @csrf.csrfProtect
    def handler(*args, **kwargs):
        called.append(True)
        return "ok"

    _fakeRequest(monkeypatch, _sameOriginHeaders(**{"Sec-Fetch-Site": "cross-site"}))
    with pytest.raises(RestException) as exc:
        handler()
    assert exc.value.code == 403
    assert called == []


def test_decorator_rejects_missing_headers(monkeypatch):
    @csrf.csrfProtect
    def handler(*args, **kwargs):
        return "ok"

    _fakeRequest(monkeypatch, {"X-Forwarded-Host": DEPLOYMENT, "X-Forwarded-Proto": "https"})
    with pytest.raises(RestException) as exc:
        handler()
    assert exc.value.code == 403
