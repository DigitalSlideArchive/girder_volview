"""Server-side CSRF defense for the facade's cookie-authenticated write routes.

VolView's session-save and ``runTask`` routes are ``@access.public(cookie=True)``,
which opts them out of Girder's built-in same-origin protection for cookie auth.
The DSA topology serves the VolView client
*same-origin* with this facade behind a reverse proxy, so we re-establish that
protection server-side and client-transparently: every state-changing handler is
wrapped so a request whose browser-set ``Origin`` / ``Sec-Fetch-Site`` headers do
not vouch for a same-origin caller is rejected before it reaches the model layer.

This Origin / ``Sec-Fetch-Site`` check is the *sole* v1 CSRF control -- the
``SameSite`` cookie rewrite and a CSP ``connect-src`` header are deferred
(deployment-layer posture). It is defense *in addition to* the Girder ACL
re-checks each write route already performs, never a replacement, and it relies
only on headers the browser sets itself, so the client needs no change. The
check is applied uniformly to the write routes; because only the same-origin
browser client calls them today, a request that arrives with no browser signal
at all is failed closed rather than special-cased by auth mode.

Functional by construction: pure decision helpers over a plain headers mapping,
plus one decorator that binds them to the live cherrypy request.
"""

import functools
from urllib.parse import urlparse

import cherrypy

from girder.exceptions import RestException


# Stamped on every wrapped handler so a test can enumerate the registered routes
# and fail if a new write route ever ships without the guard.
CSRF_PROTECTED_ATTR = "volviewCsrfProtected"

# One uninformative 403 for every rejection path -- cross-site, foreign Origin,
# or the fail-closed no-signal case -- so a probe learns nothing from the denial.
REJECT_MESSAGE = "Cross-site request blocked."

# Ports the URL spec omits from an origin; used so ``host`` and
# ``host:<defaultPort>`` compare equal.
_DEFAULT_PORTS = {"http": "80", "https": "443"}


# ---------------------------------------------------------------------------
# Origin derivation -- the browser-facing origin, never cherrypy's local bind
# ---------------------------------------------------------------------------


def _firstHop(value):
    """First entry of a possibly comma-chained ``X-Forwarded-*`` value, or None."""
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def normalizeOrigin(origin):
    """Parse an origin to ``(scheme, host, port)`` with the scheme's default port
    made explicit, so ``https://a`` and ``https://a:443`` compare equal.

    Returns ``None`` for anything that is not a usable origin (missing scheme or
    host, an opaque ``"null"`` origin, or an unparseable port), which makes such
    a value fail the equality check in :func:`isSameOriginWrite`.
    """
    if not origin:
        return None
    try:
        parsed = urlparse(origin.strip())
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if not scheme or not host:
        return None
    return (scheme, host, str(port) if port else _DEFAULT_PORTS.get(scheme, ""))


def deploymentOrigin(headers, fallbackScheme="https"):
    """The deployment's own browser-facing origin as ``(scheme, host, port)``.

    Derived from the reverse proxy's forwarded host/proto
    (``X-Forwarded-Host`` / ``X-Forwarded-Proto``) -- what the browser actually
    addressed behind the DSA reverse proxy. Falls back to the ``Host`` request
    header for the host and ``fallbackScheme`` for the scheme, but NEVER to
    cherrypy's local socket address, which behind the proxy is an internal
    upstream the browser never sees. Returns ``None`` when no host can be
    established, which makes any present ``Origin`` fail closed.
    """
    host = _firstHop(headers.get("X-Forwarded-Host")) or headers.get("Host")
    if not host:
        return None
    scheme = _firstHop(headers.get("X-Forwarded-Proto")) or fallbackScheme
    return normalizeOrigin("%s://%s" % (scheme, host))


# ---------------------------------------------------------------------------
# The decision -- pure over a headers mapping
# ---------------------------------------------------------------------------


def isSameOriginWrite(headers, fallbackScheme="https"):
    """Whether a state-changing request's browser-set headers vouch for a
    same-origin caller.

    Fail-closed rule (D9 addendum, WORKORDER chunk 3):

    * reject when ``Sec-Fetch-Site`` is ``cross-site``;
    * reject when ``Origin`` is present and is not the deployment origin;
    * reject when the request carries *neither* header (no browser signal at all
      on a cookie-auth write).

    A ``same-origin`` / ``same-site`` / ``none`` ``Sec-Fetch-Site`` together with
    a matching (or absent) ``Origin`` passes.
    """
    secFetchSite = headers.get("Sec-Fetch-Site")
    origin = headers.get("Origin")

    # No browser signal at all on a state-changing request: fail closed.
    if not secFetchSite and not origin:
        return False

    # The browser's own verdict -- reject the one value that means another
    # *site* initiated the request.
    if secFetchSite == "cross-site":
        return False

    # A present Origin must match our own. This also rejects a same-site but
    # cross-origin caller (``Sec-Fetch-Site: same-site``) and any request whose
    # ``Sec-Fetch-Site`` an intermediary stripped.
    if origin and normalizeOrigin(origin) != deploymentOrigin(headers, fallbackScheme):
        return False

    return True


# ---------------------------------------------------------------------------
# The decorator -- bind the decision to the live cherrypy request
# ---------------------------------------------------------------------------


def csrfProtect(handler):
    """Wrap a facade write handler so a cross-site / unvouched-for request is
    rejected with HTTP 403 before it reaches the model layer.

    Apply it directly beneath ``@access.public(...)`` so that decorator still
    stamps ``accessLevel`` / ``requiredScopes`` / ``cookieAuth`` onto the
    returned wrapper::

        @access.public(cookie=True, scope=TokenScope.DATA_WRITE)
        @csrfProtect
        @boundHandler
        @autoDescribeRoute(...)
        def saveToFolder(...): ...

    The wrapper carries :data:`CSRF_PROTECTED_ATTR` so the route-enumeration test
    can fail if a new write route ever ships without this guard.
    """

    @functools.wraps(handler)
    def wrapped(*args, **kwargs):
        scheme = getattr(cherrypy.request, "scheme", None) or "https"
        if not isSameOriginWrite(cherrypy.request.headers, fallbackScheme=scheme):
            raise RestException(REJECT_MESSAGE, code=403)
        return handler(*args, **kwargs)

    setattr(wrapped, CSRF_PROTECTED_ATTR, True)
    return wrapped
