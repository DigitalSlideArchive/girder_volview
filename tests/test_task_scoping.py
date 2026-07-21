"""Coverage for radiology-only task scoping.

``listTasks`` would otherwise advertise every registered slicer_cli_web CLI, so
a radiology VolView's dropdown also lists the HistomicsTK *pathology* CLIs. The
backend keeps only CLIs whose Slicer XML ``<category>`` is in an allowed set
(default radiology), server-side, and 404s a filtered-out taskId so scoping
can't be bypassed by guessing an id. Fail-closed: an unknown/absent category is
excluded.

The decorated REST handlers (``listTasks``/``getTaskSpec``/``runTask``) need a
live request context, so these drive the underlying helpers the handlers call
(``_scopedCliItems`` is the exact set ``listTasks`` advertises;
``_findScopedCliItem`` returning ``None`` is what makes ``getTaskSpec``/
``runTask`` raise 404). No live Girder.
"""

import types

import pytest

from girder_volview.backend import slicer_spec, submit


def _xml(category=None, name="Tool"):
    """A minimal Slicer Execution Model XML with an optional ``<category>``."""
    cat = f"  <category>{category}</category>\n" if category is not None else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<executable>\n"
        f"{cat}"
        f"  <title>{name}</title>\n"
        "  <description>x</description>\n"
        "</executable>\n"
    )


def _cli(name, category=None, xml=None):
    """A fake CLIItem carrying just the ``.xml``/``.name`` the scoping reads."""
    return types.SimpleNamespace(
        name=name, xml=xml if xml is not None else _xml(category, name)
    )


@pytest.fixture(autouse=True)
def _clear_categories_env(monkeypatch):
    """Default-set tests must not see a stray override from the environment."""
    monkeypatch.delenv(submit._ALLOWED_CATEGORIES_ENV, raising=False)


def test_scoped_cli_items_keeps_only_radiology(monkeypatch):
    items = [
        _cli("MedianFilter", "Radiology"),
        _cli("OtsuSegmentation", "Radiology"),
        _cli("ThresholdSegmentation", "Radiology"),
        _cli("NucleiDetection", "HistomicsTK"),  # pathology — excluded
        _cli("ColorDeconvolution", "HistomicsTK"),  # pathology — excluded
        _cli("Mystery"),  # no category — fail-closed
        _cli("Garbled", xml="<not-valid-xml"),  # unparseable — fail-closed
    ]
    monkeypatch.setattr(submit, "_listCliItems", lambda user: items)
    kept = {c.name for c in submit._scopedCliItems(user="u")}
    assert kept == {"MedianFilter", "OtsuSegmentation", "ThresholdSegmentation"}


def test_find_scoped_cli_item_resolves_only_in_scope(monkeypatch):
    catalog = {
        "rad": _cli("MedianFilter", "Radiology"),
        "path": _cli("NucleiDetection", "HistomicsTK"),
        "none": _cli("Mystery"),
    }
    monkeypatch.setattr(
        submit, "_findCliItem", lambda taskId, user: catalog.get(taskId)
    )
    # In-scope id resolves to (CLIItem, parsedCli); getTaskSpec/runTask proceed and
    # reuse the parsed structure (category confirms scope, outputs feed autofill).
    cli, parsed = submit._findScopedCliItem("rad", "u")
    assert cli.name == "MedianFilter"
    assert parsed["category"] == "Radiology"
    # Out-of-scope (pathology) and fail-closed (no category) ids resolve to None,
    # so the handlers raise the same 404 as a genuinely unknown id — no leak.
    assert submit._findScopedCliItem("path", "u") is None
    assert submit._findScopedCliItem("none", "u") is None
    assert submit._findScopedCliItem("missing", "u") is None


def test_task_in_scope_is_fail_closed_and_case_insensitive():
    assert submit._taskInScope(_cli("a", "Radiology"))
    assert submit._taskInScope(_cli("a", "radiology"))  # case-insensitive
    assert submit._taskInScope(_cli("a", " Radiology "))  # surrounding space
    assert not submit._taskInScope(_cli("a", "HistomicsTK"))
    assert not submit._taskInScope(_cli("a"))  # absent category
    assert not submit._taskInScope(_cli("a", xml="<broken"))  # unparseable


def test_cli_category_parsing():
    assert slicer_spec.parse_cli(_xml("Radiology"))["category"] == "Radiology"
    assert slicer_spec.parse_cli(_xml(" Filtering "))["category"] == "Filtering"
    assert slicer_spec.parse_cli(_xml())["category"] is None  # no <category>
    assert slicer_spec.parse_cli("")["category"] is None  # empty text
    assert slicer_spec.parse_cli("<broken")["category"] is None  # unparseable


def test_allowed_categories_default(monkeypatch):
    assert submit._allowedCategories() == {"radiology", "segmentation", "filtering"}


def test_allowed_categories_env_override(monkeypatch):
    monkeypatch.setenv(submit._ALLOWED_CATEGORIES_ENV, "Pathology, HistomicsTK")
    assert submit._allowedCategories() == {"pathology", "histomicstk"}


def test_empty_override_falls_back_to_default_not_unfiltered(monkeypatch):
    monkeypatch.setenv(submit._ALLOWED_CATEGORIES_ENV, "  , ")
    assert submit._allowedCategories() == {"radiology", "segmentation", "filtering"}


def test_env_override_rescopes_catalog(monkeypatch):
    items = [
        _cli("MedianFilter", "Radiology"),
        _cli("NucleiDetection", "HistomicsTK"),
    ]
    monkeypatch.setattr(submit, "_listCliItems", lambda user: items)
    monkeypatch.setenv(submit._ALLOWED_CATEGORIES_ENV, "HistomicsTK")
    kept = {c.name for c in submit._scopedCliItems(user="u")}
    assert kept == {"NucleiDetection"}  # radiology now out of scope, pathology in
