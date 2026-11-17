"""Tests for `topology.hierarchy_mismatch`'s inverse direction (issue #5756).

`klt lvs` has disclosed the flat-layout-vs-hierarchical-reference asymmetry
since issue #4548 (``side: "reference"``, tests in ``test_lvs.py``). These
tests cover the inverse the default-on ``options.flatten_reference`` actually
produces: a *hierarchical* layout netlist (the normal shape for a routed
block's extracted subcircuits) against a reference the tool itself flattened
to a single circuit -- the shape that otherwise reports one generic
null-detail ``topology`` "circuit could not be matched" error per layout
circuit and nothing naming the structural cause. The fixtures are the same
divider pair ``test_lvs.py`` uses for the #4548 direction, sides swapped.
"""

from __future__ import annotations

import json
from pathlib import Path

from klayout_tools import lvs
from klayout_tools.lvs import run_lvs

#: Two chained instances of a one-resistor leaf, both declared -- the
#: hierarchical form. `top`'s three pins (P1/P2/P3) chain X1's B pin to X2's
#: A pin through the shared net P2.
_HIERARCHICAL_DIVIDER_SPICE = """
.subckt leaf A B
R1 A B 1k
.ends
.subckt top P1 P2 P3
X1 P1 P2 leaf
X2 P2 P3 leaf
.ends
"""

#: The same two-resistor chain, already flattened -- exactly what the
#: reference side becomes after `options.flatten_reference` (or a
#: hand-written flat netlist).
_FLAT_DIVIDER_SPICE = """
.subckt top P1 P2 P3
R1 P1 P2 1k
R2 P2 P3 1k
.ends
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request))
    return str(path)


def _hierarchy_mismatch_entries(report: dict) -> list[dict]:
    return [
        m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_TOPOLOGY_HIERARCHY_MISMATCH
    ]


def _run_divider(
    tmp_path: Path, layout_spice: str, reference_spice: str, options: dict | None = None
) -> dict:
    request = {
        "layout": {
            "netlist": _write(tmp_path / "layout.spice", layout_spice),
            "top": "top",
        },
        "reference": {
            "netlist": _write(tmp_path / "ref.spice", reference_spice),
            "top": "top",
        },
    }
    if options is not None:
        request["options"] = options
    return run_lvs(_write_request(tmp_path / "request.json", request))


def test_inverse_warning_names_flatten_layout_against_an_already_flat_reference(
    tmp_path,
):
    """Issue #5756: hierarchical layout + single-circuit reference with
    `flatten_reference` set -> exactly one advisory warning on the layout
    side naming the one-option fix; `status` stays the comparer's own
    mismatch verdict."""
    report = _run_divider(
        tmp_path,
        _HIERARCHICAL_DIVIDER_SPICE,
        _FLAT_DIVIDER_SPICE,
        {"flatten_reference": True},
    )

    assert report["status"] == "mismatch"
    [entry] = _hierarchy_mismatch_entries(report)
    assert entry["severity"] == "warning"
    assert entry["side"] == "layout"
    assert "hierarchical (2 circuits)" in entry["description"]
    assert "single circuit" in entry["description"]
    assert "options.flatten_reference" in entry["description"]
    assert "options.flatten_layout: true" in entry["description"]
    assert report["category_counts"][lvs.CATEGORY_TOPOLOGY_HIERARCHY_MISMATCH] == 1


def test_inverse_warning_fires_when_the_tool_flattened_the_reference_itself(
    tmp_path,
):
    """The measured shape from the issue: a hierarchical reference the
    default-on `flatten_reference` collapsed to one circuit (disclosed
    separately as `topology.flattened`) against a hierarchical layout that
    kept its circuits -- both disclosures appear, exactly one
    hierarchy-mismatch entry between them."""
    report = _run_divider(
        tmp_path,
        _HIERARCHICAL_DIVIDER_SPICE,
        _HIERARCHICAL_DIVIDER_SPICE,
        {"flatten_reference": True},
    )

    assert report["status"] == "mismatch"
    [entry] = _hierarchy_mismatch_entries(report)
    assert entry["severity"] == "warning"
    assert entry["side"] == "layout"
    assert "options.flatten_layout: true" in entry["description"]
    assert report["category_counts"]["topology.flattened"] == 1
    flattened = [
        m for m in report["mismatches"] if m["category"] == "topology.flattened"
    ]
    assert [m["side"] for m in flattened] == ["reference"]


def test_inverse_warning_absent_when_flatten_layout_also_set(tmp_path):
    """The documented recipe (`flatten_layout: true` + `flatten_reference:
    true`) pairs everything, so the advisory must not fire on the `match`."""
    report = _run_divider(
        tmp_path,
        _HIERARCHICAL_DIVIDER_SPICE,
        _HIERARCHICAL_DIVIDER_SPICE,
        {"flatten_reference": True, "flatten_layout": True},
    )

    assert report["status"] == "match"
    assert _hierarchy_mismatch_entries(report) == []


def test_inverse_warning_absent_when_both_flat(tmp_path):
    report = _run_divider(
        tmp_path,
        _FLAT_DIVIDER_SPICE,
        _FLAT_DIVIDER_SPICE,
        {"flatten_reference": True},
    )

    assert _hierarchy_mismatch_entries(report) == []


def test_inverse_warning_absent_when_both_hierarchical_and_no_flatten(tmp_path):
    report = _run_divider(
        tmp_path, _HIERARCHICAL_DIVIDER_SPICE, _HIERARCHICAL_DIVIDER_SPICE
    )

    assert _hierarchy_mismatch_entries(report) == []


def test_inverse_warning_absent_when_flatten_reference_unset(tmp_path):
    """With `flatten_reference` unset the reference was not flattened by the
    tool, so the inverse advisory stays quiet even though the sides differ
    (and the #4548 direction needs a *flat* layout, which this is not)."""
    report = _run_divider(tmp_path, _HIERARCHICAL_DIVIDER_SPICE, _FLAT_DIVIDER_SPICE)

    assert _hierarchy_mismatch_entries(report) == []
