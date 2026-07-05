"""The vendored processing-contract copy cannot silently drift (Chunk 29;
ARCHITECTURE-REVIEW §4.6 "No cross-repo drift check on the vendored contract
copy", §5.3 checklist item 3).

``scripts/sync-facade.sh`` (in VolView) regenerates the contract, copies
``fixtures/`` + ``generated/`` here, and writes ``MANIFEST.sha256`` over every
copied file. This suite re-hashes the vendored tree against that manifest, so BOTH
failure modes are caught, fail-closed:

* a hand-edited (or otherwise tampered) vendored fixture/schema — its content no
  longer matches the recorded digest;
* a stale / partial sync — a file copied but not re-manifested (present but
  unlisted), or a manifest entry whose file never arrived (listed but missing).

Re-running ``sync-facade.sh`` regenerates from the single source and rewrites the
manifest, so a legitimate contract change flows through cleanly. Pure-stdlib
(hashlib): no Girder / Mongo.
"""

import hashlib

import contract_loader


_MANIFEST_PATH = contract_loader.CONTRACT_ROOT / "MANIFEST.sha256"

# The subtrees sync-facade.sh copies (and hashes). The manifest lives at the
# tests/contract root, OUTSIDE these, so it never hashes itself.
_MANIFESTED_SUBDIRS = ("fixtures", "generated")


def _parse_manifest(text):
    """Parse ``sha256sum`` output into ``{relative_path: hex_digest}``.

    Coreutils writes ``<hexdigest><space><mode><path>`` where mode is a space
    (text) or ``*`` (binary); tolerate either.
    """
    entries = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, rest = line.split(" ", 1)
        path = rest.lstrip(" *")
        entries[path] = digest
    return entries


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _actual_vendored_files():
    """Every vendored file under the manifested subtrees, as posix relpaths."""
    found = set()
    for subdir in _MANIFESTED_SUBDIRS:
        root = contract_loader.CONTRACT_ROOT / subdir
        for path in root.rglob("*"):
            if path.is_file():
                found.add(path.relative_to(contract_loader.CONTRACT_ROOT).as_posix())
    return found


def test_manifest_present_and_nonempty():
    # The manifest is written by sync-facade.sh; its absence means the vendored
    # copy was placed WITHOUT the drift guard — fail closed.
    assert _MANIFEST_PATH.exists(), (
        "MANIFEST.sha256 missing; re-run processing-contract/scripts/sync-facade.sh"
    )
    entries = _parse_manifest(_MANIFEST_PATH.read_text())
    assert entries, "MANIFEST.sha256 is empty"


def test_every_manifest_entry_matches_the_vendored_file():
    # Detects a hand-edited / tampered fixture or schema (AC3) and a listed-but-
    # missing file (a partial sync).
    entries = _parse_manifest(_MANIFEST_PATH.read_text())
    for rel_path, expected_digest in entries.items():
        target = contract_loader.CONTRACT_ROOT / rel_path
        assert target.exists(), "manifest lists a file that is not vendored: %s" % rel_path
        actual = _sha256(target)
        assert actual == expected_digest, (
            "vendored file drifted from MANIFEST.sha256 (edited or stale sync?): %s"
            % rel_path
        )


def test_manifest_covers_exactly_the_vendored_files():
    # Bidirectional: an extra vendored file the manifest does not list is a stale/
    # partial sync too (the copy grew without re-manifesting), so require set
    # equality — not just that every listed file is present.
    listed = set(_parse_manifest(_MANIFEST_PATH.read_text()))
    actual = _actual_vendored_files()
    assert listed == actual, {
        "unlisted_extra_files": sorted(actual - listed),
        "missing_listed_files": sorted(listed - actual),
    }
