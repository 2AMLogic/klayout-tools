"""Coverage grading for a DRC rule narrowed by `layer_selection` (issue #5435).

`klt drc` decided applicability from layer *presence*, before a rule's own
`layer_selection` narrowing was applied, so a rule that selected away all of
its layer's geometry still graded `checked` -- a `clean` verdict naming a rule
that measured zero polygons. These tests pin the corrected grading
(`inapplicable` / `inapplicable_empty_checked_region`), the cases that must
keep grading exactly as before, and the two check-kind carve-outs where an
empty primary region can still report a violation.

The measurement half of `layer_selection` (which polygons a selection keeps)
lives in `tests/test_drc.py`; this module is only about what the coverage
block says about it.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from klayout_tools.drc_check_kinds import (
    _ANTENNA_CHECKS,
    _AREA_CHECKS,
    _COMP_COVERAGE_CHECKS,
    _DENSITY_CHECKS,
    _SINGLE_LAYER_CHECKS,
    _TAP_DISTANCE_CHECKS,
    _TWO_LAYER_CHECKS,
    _VERTEX_CHECKS,
)
from klayout_tools.drc_layer_selection import SELECTION_VACUOUS_CHECKS

# `tests/test_drc.py` shadows pytest's `tmp_path` to seed a `.git` marker so
# the layout resolves as a repo-relative input; nothing here reads the `file`
# envelope, so the plain fixture is enough.


def _patch_synthetic_deck(monkeypatch, rules):
    """Wire `run_drc(..., "synthetic")` to a one-off `rules` list -- the same
    monkeypatch triple `tests/test_drc.py` uses."""
    monkeypatch.setattr("klayout_tools.drc.get_deck", lambda name: rules)
    monkeypatch.setattr("klayout_tools.drc.get_nominal_dbu", lambda name: 0.001)
    monkeypatch.setattr("klayout_tools.drc.get_layer_names", lambda name: {})


def _cont_layout(tmp_path: Path, name: str, boxes) -> str:
    """A one-top-cell sg13g2 stream drawing `boxes` on Cont (6/0)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    cont = layout.layer(6, 0)
    layout.set_info(cont, kdb.LayerInfo(6, 0, "Cont.drawing"))
    for box in boxes:
        top.shapes(cont).insert(box)
    path = tmp_path / f"{name}.gds"
    layout.write(str(path))
    return str(path)


# `layer_selection` decides coverage, not just measurement (issue #5435): a
# rule whose selection empties its input measured zero polygons, so it is
# graded `inapplicable` rather than `checked`. sg13g2's two
# `layer_selection="squares"` Cont rules are the deck's only users.
_SG13G2_SELECTION_RULES = ("cont.width.1", "cont.space.1")


def _coverage_reasons(report):
    """`{rule id: inapplicable reason}` for one report's coverage block."""
    return {
        record["id"]: record["reason"] for record in report["coverage"]["inapplicable"]
    }


def test_run_drc_sg13g2_selection_emptied_rule_is_inapplicable_not_checked(tmp_path):
    """A stream whose whole Cont population is a non-square bar empties both
    `layer_selection="squares"` rules' input, so neither may be reported as
    checked: each is `inapplicable` with reason
    `inapplicable_empty_checked_region` (issue #5435).

    This is the issue's own reproducer: before the fix both rules appeared in
    `coverage.rules_checked` alongside a `clean` verdict, having measured zero
    polygons, with no coverage field a consumer could consult to notice.
    """
    path = _cont_layout(tmp_path, "selection_empty_bar", [kdb.Box(0, 0, 160, 400)])

    report = run_drc(path, "sg13g2")
    coverage = report["coverage"]
    reasons = _coverage_reasons(report)

    for rule_id in _SG13G2_SELECTION_RULES:
        assert rule_id not in coverage["rules_checked"], rule_id
        assert rule_id not in coverage["checked"], rule_id
        assert rule_id in coverage["rules_skipped"], rule_id
        assert reasons.get(rule_id) == "inapplicable_empty_checked_region", rule_id
        # An empty selected region is not a *requested* check that failed to
        # run, so it must not land in the partial-making `skipped` list.
        assert rule_id not in {record["id"] for record in coverage["skipped"]}, rule_id

    # Inapplicable work never makes a run partial, and the bar itself is
    # legal: the verdict stays an unconditional `clean`.
    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    # `layers_checked` is the deck-layer n stream-layer set, not per-rule:
    # 6/0 is still legitimately read by `cont.ongrid.1`, which has no
    # selection and is still checked.
    assert "6/0" in coverage["layers_checked"]
    assert "cont.ongrid.1" in coverage["rules_checked"]


def test_run_drc_sg13g2_selection_with_one_square_stays_checked(tmp_path):
    """One legal square Cont alongside a bar leaves the selected region
    non-empty, so both selection rules stay `checked` and the stream is still
    clean (issue #5435)."""
    path = _cont_layout(
        tmp_path,
        "selection_square_and_bar",
        [kdb.Box(0, 0, 160, 160), kdb.Box(1000, 0, 1160, 400)],
    )

    report = run_drc(path, "sg13g2")
    coverage = report["coverage"]
    reasons = _coverage_reasons(report)

    for rule_id in _SG13G2_SELECTION_RULES:
        assert rule_id in coverage["rules_checked"], rule_id
        assert rule_id not in coverage["rules_skipped"], rule_id
        assert rule_id not in reasons, rule_id
    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sg13g2_selection_rule_still_reports_violations(tmp_path):
    """The grading only moves a rule that measured *nothing*: an oversized
    square Cont is still a `cont.width.1` violation, reported from a
    `checked` rule (issue #5435)."""
    path = _cont_layout(
        tmp_path,
        "selection_oversized_square",
        [kdb.Box(0, 0, 200, 200), kdb.Box(1000, 0, 1160, 400)],
    )

    report = run_drc(path, "sg13g2")

    assert report["status"] == "violations"
    assert report["rule_counts"].get("cont.width.1") == 1
    assert "cont.width.1" in report["coverage"]["rules_checked"]


def test_run_drc_sg13g2_absent_selection_layer_still_no_applicable_geometry(tmp_path):
    """With Cont absent from the stream entirely, the selection rules keep
    reporting `no_applicable_geometry` -- the layer-presence answer is
    unchanged by #5435, and it is the *more* informative reason when there is
    no geometry on the layer at all."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    gatpoly = layout.layer(5, 0)
    layout.set_info(gatpoly, kdb.LayerInfo(5, 0, "GatPoly.drawing"))
    top.shapes(gatpoly).insert(kdb.Box(0, 0, 500, 500))
    path = tmp_path / "sg13g2_no_cont.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")
    reasons = _coverage_reasons(report)

    for rule_id in _SG13G2_SELECTION_RULES:
        assert reasons.get(rule_id) == "no_applicable_geometry", rule_id
    assert report["status"] == "clean"


@pytest.mark.parametrize(
    "name,second_top_boxes,expect_checked",
    [
        # One top cell draws a legal square: the rule measured real geometry
        # somewhere in the stream, so it stays checked.
        ("square_in_second_top", [kdb.Box(0, 0, 160, 160)], True),
        # Both top cells are all-bars: empty in every top cell.
        ("bar_in_second_top", [kdb.Box(0, 0, 160, 400)], False),
    ],
)
def test_run_drc_sg13g2_selection_grading_spans_every_top_cell(
    name, second_top_boxes, expect_checked, tmp_path
):
    """The grading is "empty in *every* top cell", matching the per-top-cell
    region the check itself measures (issue #5435)."""
    layout = kdb.Layout()
    cont = layout.layer(6, 0)
    layout.set_info(cont, kdb.LayerInfo(6, 0, "Cont.drawing"))
    first = layout.create_cell("TOP1")
    first.shapes(cont).insert(kdb.Box(0, 0, 160, 400))  # a bar
    second = layout.create_cell("TOP2")
    for box in second_top_boxes:
        second.shapes(cont).insert(box)
    path = tmp_path / f"{name}.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")
    coverage = report["coverage"]
    reasons = _coverage_reasons(report)

    for rule_id in _SG13G2_SELECTION_RULES:
        if expect_checked:
            assert rule_id in coverage["rules_checked"], rule_id
        else:
            assert rule_id not in coverage["rules_checked"], rule_id
            assert reasons.get(rule_id) == "inapplicable_empty_checked_region", rule_id
    assert report["status"] == "clean"


def test_run_drc_sg13g2_abutting_squares_merging_to_a_strip_are_inapplicable(tmp_path):
    """The selection runs on the *merged* region, so a row of abutting legal
    contacts is one non-square strip -- the rule measured nothing and is
    inapplicable, not a clean check (issues #3139, #5435)."""
    path = _cont_layout(
        tmp_path,
        "selection_abutting_row",
        [kdb.Box(i * 160, 0, (i + 1) * 160, 160) for i in range(4)],
    )

    report = run_drc(path, "sg13g2")
    reasons = _coverage_reasons(report)

    for rule_id in _SG13G2_SELECTION_RULES:
        assert reasons.get(rule_id) == "inapplicable_empty_checked_region", rule_id
    assert report["status"] == "clean"
    assert report["rule_counts"].get("cont.width.1", 0) == 0


def test_run_drc_selection_emptied_require_containment_enclosing_stays_checked(
    tmp_path, monkeypatch
):
    """An `"enclosing"` rule with `require_containment` is excluded from the
    #5435 grading: there an empty primary region *is* the violation -- every
    cut unlanded on its conductor (#2726) -- so the rule must stay checked and
    keep reporting.

    Guards the "confirm per check kind, do not assume" half of the fix: a
    blanket "empty selected region means nothing was measured" would convert
    this real finding into a clean verdict.
    """
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="conductor.enclosing.cut.1",
                description="synthetic: every cut must land on a square conductor",
                layer=(40, 0),  # the conductor (primary region)
                other_layer=(41, 0),  # the cut
                check="enclosing",
                threshold_dbu=50,
                require_containment=True,
                layer_selection="squares",
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    # The conductor is drawn, but only as a bar -- the "squares" selection
    # empties the primary region.
    top.shapes(layout.layer(40, 0)).insert(kdb.Box(0, 0, 200, 800))
    # A cut sitting far from any (selected) conductor: unlanded.
    top.shapes(layout.layer(41, 0)).insert(kdb.Box(5000, 5000, 5100, 5100))
    path = tmp_path / "require_containment_selection.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert "conductor.enclosing.cut.1" in report["coverage"]["rules_checked"]
    assert report["status"] == "violations"
    assert report["rule_counts"].get("conductor.enclosing.cut.1", 0) >= 1


def test_run_drc_selection_emptied_net_space_stays_checked(tmp_path, monkeypatch):
    """`check="net_space"` is excluded from the #5435 grading: it rebuilds its
    own regions from the layout and never reads the selected primary region,
    so an empty selection says nothing about what it measured."""
    from klayout_tools.decks import DrcRule, NetSpaceSpec

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="metal.net_space.1",
                description="synthetic: different-net metal spacing",
                layer=(30, 0),
                check="net_space",
                threshold_dbu=300,
                layer_selection="squares",
                net_spec=NetSpaceSpec(tap_and=((31, 0),), chain=((32, 0),)),
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    # Two metal bars (no squares -> the selection empties the primary region)
    # on different nets, 100 dbu apart: under the 300 dbu threshold.
    top.shapes(layout.layer(30, 0)).insert(kdb.Box(0, 0, 200, 800))
    top.shapes(layout.layer(30, 0)).insert(kdb.Box(300, 0, 500, 800))
    path = tmp_path / "net_space_selection.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert "metal.net_space.1" in report["coverage"]["rules_checked"]
    assert "metal.net_space.1" not in report["coverage"]["rules_skipped"]


def test_run_drc_selection_grading_only_applies_to_selection_rules(
    tmp_path, monkeypatch
):
    """A rule with no `layer_selection` is graded exactly as before, from
    layer presence: a drawn-but-passing layer stays `checked`, never
    `inapplicable` (issue #5435 changes nothing for it)."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="metal.width.1",
                description="synthetic: minimum metal width, no selection",
                layer=(30, 0),
                check="width",
                threshold_dbu=100,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(30, 0)).insert(kdb.Box(0, 0, 200, 800))
    path = tmp_path / "no_selection.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["coverage"]["rules_checked"] == ["metal.width.1"]
    assert report["coverage"]["inapplicable"] == []
    assert report["status"] == "clean"


def test_selection_vacuous_checks_is_every_dispatched_kind_but_the_two_carve_outs():
    """Structural guard on the #5435 vacuity table: it must name every check
    kind `_evaluate_rule` can still dispatch at the grading point, minus the
    two that are deliberately excluded.

    `"net_space"` rebuilds its own regions from the layout and never reads
    the selected primary region, so an empty selection says nothing about
    what it measured; the region kinds (`"comp_coverage"`/`"tap_distance"`)
    are graded earlier by `_dispatch_region_rule`, with this same reason
    token, and never reach the grading. A check kind added later lands in
    neither set and trips this test rather than silently grading as checked.
    """
    dispatched = (
        _SINGLE_LAYER_CHECKS
        | _TWO_LAYER_CHECKS
        | _AREA_CHECKS
        | _DENSITY_CHECKS
        | _VERTEX_CHECKS
        | _ANTENNA_CHECKS
        | _COMP_COVERAGE_CHECKS
        | _TAP_DISTANCE_CHECKS
        | {"net_space"}
    )
    excluded = _COMP_COVERAGE_CHECKS | _TAP_DISTANCE_CHECKS | {"net_space"}

    assert SELECTION_VACUOUS_CHECKS == dispatched - excluded
