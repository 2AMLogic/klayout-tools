"""Production-path tests for gf180mcu ``comp.coverage.1`` (DF.12), issue #3587.

Mirrors the DF.12 fixtures of ``tests/helpers/gf180mcu_comp_spike.py`` against
``run_drc(..., "gf180mcu")`` and pins the ``comp_coverage`` bookkeeping
(checked vs. inapplicable) and descriptor validation.
"""

from __future__ import annotations

import pytest

from golden_deck.manifest import write_layout
from klayout_tools.decks import CompCoverageSpec
from klayout_tools.drc import run_drc

RULE = "comp.coverage.1"
COMP, NPLUS, PPLUS, SCHOTTKY = (22, 0), (32, 0), (31, 0), (241, 0)
BIG = [0, 0, 4000, 2000]


def _run(tmp_path, shapes):
    path = write_layout(
        [{"layer": list(layer), "box": box} for layer, box in shapes],
        tmp_path / "f.gds",
    )
    return run_drc(path, "gf180mcu")


def _by_rule(report):
    return [v for v in report["violations"] if v["rule"] == RULE]


def test_bare_comp_violates(tmp_path):
    report = _run(tmp_path, [(COMP, BIG)])
    found = _by_rule(report)
    assert len(found) == 1
    assert found[0]["check"] == "comp_coverage"
    assert found[0]["bbox"] == {"left": 0, "bottom": 0, "right": 4000, "top": 2000}
    assert RULE in report["coverage"]["rules_checked"]


def test_fully_implanted_comp_is_clean(tmp_path):
    report = _run(tmp_path, [(COMP, BIG), (NPLUS, [-1000, -1000, 5000, 3000])])
    assert _by_rule(report) == []
    assert RULE in report["coverage"]["rules_checked"]


def test_partial_nplus_reports_only_uncovered_piece(tmp_path):
    report = _run(tmp_path, [(COMP, BIG), (NPLUS, [-1000, -1000, 2000, 3000])])
    found = _by_rule(report)
    assert len(found) == 1
    assert found[0]["bbox"]["left"] == 2000 and found[0]["bbox"]["right"] == 4000


def test_joint_n_and_p_implants_cover(tmp_path):
    report = _run(
        tmp_path,
        [
            (COMP, BIG),
            (NPLUS, [-1000, -1000, 2000, 3000]),
            (PPLUS, [2000, -1000, 5000, 3000]),
        ],
    )
    assert _by_rule(report) == []


def test_partial_marker_exempts_whole_polygon(tmp_path):
    report = _run(
        tmp_path,
        [
            (COMP, BIG),
            (NPLUS, [-1000, -1000, 1000, 3000]),
            (SCHOTTKY, [3000, -1000, 5000, 3000]),
        ],
    )
    assert _by_rule(report) == []


def test_marker_edge_touch_exempts(tmp_path):
    report = _run(tmp_path, [(COMP, BIG), (SCHOTTKY, [4000, -1000, 6000, 3000])])
    assert _by_rule(report) == []


def test_comp_absent_is_inapplicable(tmp_path):
    report = _run(tmp_path, [(NPLUS, [0, 0, 1000, 1000])])
    cov = report["coverage"]
    assert RULE in cov["rules_skipped"] and RULE not in cov["rules_checked"]
    assert {"id": RULE, "reason": "no_applicable_geometry"} in cov["inapplicable"]


def test_all_comp_under_marker_is_inapplicable_empty_region(tmp_path):
    report = _run(tmp_path, [(COMP, BIG), (SCHOTTKY, [-100, -100, 4100, 2100])])
    cov = report["coverage"]
    assert _by_rule(report) == []
    assert RULE not in cov["rules_checked"]
    assert {"id": RULE, "reason": "inapplicable_empty_checked_region"} in cov[
        "inapplicable"
    ]


def test_spec_requires_a_covering_layer():
    with pytest.raises(ValueError, match="covered_by_any"):
        CompCoverageSpec(covered_by_any=())
