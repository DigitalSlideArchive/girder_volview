"""Loader for VolView's backend-contract golden fixtures + generated JSON
Schemas.

The contract is the ``backend-contract`` subtree of the installed ``volview``
package and is the ONE normative source; the backend never keeps its own copy.
``GIRDER_VOLVIEW_CONTRACT_DIR`` overrides the location. The chosen root must
carry the ``generated/`` schemas or the import fails loudly — the conformance
kit is a gate that must never silently self-skip.

Pure stdlib (no girder import) so it loads without a running Girder/Mongo.
"""

import json
import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent

_INSTALLED_CONTRACT = (
    _REPO_ROOT
    / "girder_volview"
    / "web_client"
    / "node_modules"
    / "volview"
    / "backend-contract"
)

_ENV_VAR = "GIRDER_VOLVIEW_CONTRACT_DIR"
_SCHEMA_SUFFIX = ".schema.json"


def _resolve_contract_root():
    """Locate the ``backend-contract`` tree, or fail with a fix-it message.

    Prefer an explicit ``GIRDER_VOLVIEW_CONTRACT_DIR`` override, else the
    installed ``volview`` package. A set-but-wrong override fails loud pointing
    at itself, never silently falling back to the package.
    """
    env_dir = os.environ.get(_ENV_VAR)
    root = Path(env_dir).expanduser() if env_dir else _INSTALLED_CONTRACT
    if (root / "generated").is_dir():
        return root.resolve()

    raise RuntimeError(
        "VolView backend-contract not found under %s. The backend reads the "
        "contract from the `volview` package, not a vendored copy. Fix by "
        "either:\n"
        "  * installing the client:  npm --prefix girder_volview/web_client install\n"
        "  * linking a local VolView checkout:  "
        "cd <VolView> && npm link && "
        "npm --prefix girder_volview/web_client link volview\n"
        "  * or setting %s=<path-to>/backend-contract"
        % (root, _ENV_VAR)
    )


CONTRACT_ROOT = _resolve_contract_root()
FIXTURES_ROOT = CONTRACT_ROOT / "fixtures"
GENERATED_ROOT = CONTRACT_ROOT / "generated"
OPENAPI_PATH = GENERATED_ROOT / "openapi.json"


def load_openapi():
    """Load the published neutral OpenAPI document."""
    return json.loads(OPENAPI_PATH.read_text())


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
