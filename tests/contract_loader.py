"""Loader for the vendored processing-contract golden fixtures + generated JSON
Schemas.

The fixtures under ``tests/contract/`` are a copy of VolView's
``processing-contract`` package, synced by that package's
``scripts/sync-facade.sh`` (D4: one normative source in VolView, a synced copy
here — never hand-edited). This module is pure stdlib (no girder import) so it
loads without a running Girder/Mongo.

Chunk 5 only wires the loader (the facade test suite must LOAD the same
fixtures); the conformance assertions that validate facade-emitted specs /
intents / statuses against these fixtures + generated schemas arrive in Chunk 6.
"""

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CONTRACT_ROOT = _HERE / "contract"
FIXTURES_ROOT = CONTRACT_ROOT / "fixtures"
GENERATED_ROOT = CONTRACT_ROOT / "generated"

_SCHEMA_SUFFIX = ".schema.json"


def load_fixture(rel_path):
    """Load a single fixture by path relative to the fixtures root."""
    return json.loads((FIXTURES_ROOT / rel_path).read_text())


def load_fixture_dir(rel_dir):
    """Load every ``*.json`` fixture in a directory, keyed by file stem."""
    directory = FIXTURES_ROOT / rel_dir
    return {
        path.stem: json.loads(path.read_text())
        for path in sorted(directory.glob("*.json"))
    }


def load_generated_schema(name):
    """Load a generated JSON Schema by name (e.g. ``task-spec``)."""
    return json.loads((GENERATED_ROOT / (name + _SCHEMA_SUFFIX)).read_text())


def list_generated_schemas():
    """Names of the generated JSON Schemas (without the ``.schema.json``)."""
    return sorted(
        path.name[: -len(_SCHEMA_SUFFIX)]
        for path in GENERATED_ROOT.glob("*" + _SCHEMA_SUFFIX)
    )
