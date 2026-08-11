"""script/clean-build-tree must stop a stale build/ from contaminating a wheel.

`build_py` copies current sources into `build/lib` but never removes files that
have since left the source tree, so a `build/` carried across a branch switch
makes `bdist_wheel` ship modules the checkout no longer has -- and the install
reports success. Relocating the build directory does not help; only clearing it
does. These tests pin that difference.
"""

import glob
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLEANER = REPO_ROOT / "script" / "clean-build-tree"


def _checkout(destination):
    shutil.copytree(
        REPO_ROOT,
        destination,
        ignore=shutil.ignore_patterns(
            ".git", ".tox", "build", "*.egg-info", "node_modules", "__pycache__"
        ),
    )
    return destination


def _build(source, wheel_dir):
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel",
            "--no-deps", "--no-build-isolation", str(source), "-w", str(wheel_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return zipfile.ZipFile(glob.glob(str(wheel_dir / "*.whl"))[0]).namelist()


def test_stale_build_ships_deleted_modules_without_the_cleaner(tmp_path):
    """The hazard itself. If this ever stops failing, the cleaner is moot."""
    source = _checkout(tmp_path / "tree")
    _build(source, tmp_path / "w1")

    (source / "girder_volview" / "dicom.py").unlink()
    names = _build(source, tmp_path / "w2")

    assert any(n.endswith("girder_volview/dicom.py") for n in names), (
        "expected the stale build/ to still ship the deleted module; if setuptools "
        "now prunes build/lib, script/clean-build-tree is no longer needed"
    )


def test_cleaner_prevents_deleted_modules_from_shipping(tmp_path):
    source = _checkout(tmp_path / "tree")
    _build(source, tmp_path / "w1")

    (source / "girder_volview" / "dicom.py").unlink()
    cleaned = subprocess.run(
        [str(CLEANER), str(source)], capture_output=True, text=True
    )
    assert cleaned.returncode == 0, cleaned.stderr
    names = _build(source, tmp_path / "w2")

    assert not any(n.endswith("girder_volview/dicom.py") for n in names), (
        "a module deleted from the checkout still reached the wheel after "
        "clean-build-tree ran"
    )


def test_cleaner_is_a_no_op_on_a_checkout_with_nothing_to_clean(tmp_path):
    """Deploy runs this unconditionally, so an already-clean tree must succeed."""
    source = _checkout(tmp_path / "tree")

    result = subprocess.run([str(CLEANER), str(source)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
