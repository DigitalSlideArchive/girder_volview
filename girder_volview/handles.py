"""The proxiable load-handle scheme -- the one mint + parse pair.

The client-visible handle is ``/<apiRoot>/file/<id>/proxiable/<name>``. Mint
and parse live together here because the name segment must be escaped
identically on both sides: it is percent-encoded at mint
(``urllib.parse.quote(name, safe="")``, which escapes a strict superset of
what JS ``encodeURIComponent`` does -- it also escapes ``!*'()``) and
unescaped at parse, so
``parseFileHandle(mintFileHandle(fileId, name)) == (fileId, name)`` for every
legal name and the emitted handle carries no raw fragment/query delimiter.
Clients never decode: a handle is opaque, round-tripped byte-for-byte; only
the backend reads its own mint.

Parse also accepts a tail carrying the RAW name (spaces, ``#``, ``?``), the
shape stamped on job records and held in live clients' echoes; both shapes
canonicalize to the same file id. Genuinely foreign shapes (wrong prefix,
wrong resource, extra path segment, empty name, non-ObjectId id) stay
rejected, so callers fail closed.
"""

from urllib.parse import quote, unquote

from bson.objectid import ObjectId
from girder.utility.server import getApiRoot

_PROXIABLE_MARKER = "proxiable/"


def mintFileHandle(fileId, name):
    """Mint the proxiable load handle for a file id + backend file name.

    Origin-relative, keyed off the runtime ``getApiRoot()`` mount; the name
    segment is percent-encoded (RFC 3986, no safe characters) so reserved
    URL delimiters in legal Girder file names survive every wire context.
    """
    return "/" + "/".join(
        (
            getApiRoot(),
            "file",
            str(fileId),
            "proxiable",
            quote(str(name), safe=""),
        )
    )


def parseFileHandle(uri):
    """``(fileId, name)`` for a backend-minted load handle, or ``None``.

    The exact mirror of :func:`mintFileHandle` -- origin-relative
    ``/<apiRoot>/file/<24-hex-id>/proxiable/<name>`` against the same
    ``getApiRoot()`` mount, with the name segment unescaped. The tail must
    be one non-empty segment (no embedded ``/``), but is otherwise taken
    verbatim so legacy raw-name mints still resolve (see module docstring).
    Anything else returns ``None`` so callers fail closed and never
    dereference a foreign string.
    """
    if not isinstance(uri, str):
        return None
    prefix = "/" + getApiRoot() + "/file/"
    if not uri.startswith(prefix):
        return None
    fileId, sep, tail = uri[len(prefix) :].partition("/")
    if not sep or not ObjectId.is_valid(fileId):
        return None
    if not tail.startswith(_PROXIABLE_MARKER):
        return None
    name = tail[len(_PROXIABLE_MARKER) :]
    if not name or "/" in name:
        return None
    return fileId, unquote(name)
