"""Production-path tests for gf180mcu ``DF.13_LV`` / ``DF.14_LV``
(``comp.tap_distance.{nwell,psub}.lv.1``), issue #3607, and their ``_MV``
siblings (``comp.tap_distance.{nwell,psub}.mv.1``, 15 um), issue #3793.

Runs ``run_drc(..., "gf180mcu")`` and pins the ``tap_distance`` descriptor
validation, the zero-tap / boundary / well-island semantics derived from the
pinned upstream deck (``docs/design/gf180mcu-comp-checks-spike.md``), and the
checked-vs-inapplicable-vs-skipped bookkeeping.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.decks import TapDistanceSpec
from klayout_tools.drc import run_drc

NWELL_RULE = "comp.tap_distance.nwell.lv.1"
PSUB_RULE = "comp.tap_distance.psub.lv.1"
NWELL_MV_RULE = "comp.tap_distance.nwell.mv.1"
PSUB_MV_RULE = "comp.tap_distance.psub.mv.1"
COMP, NWELL, DNWELL, NPLUS, PPLUS = (22, 0), (21, 0), (12, 0), (32, 0), (31, 0)
DUALGATE, V5_XTOR, RES_MK = (55, 0), (112, 1), (110, 5)
D = 20000  # 20 um at the deck's 1 nm dbu
D_MV = 15000  # 15 um (5V/6V column)
PAD = 300


def _write(tmp_path, shapes, dbu=0.001):
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    for layer, box in shapes:
        top.shapes(layout.layer(*layer)).insert(kdb.Box(*box))
    path = tmp_path / "f.gds"
    layout.write(str(path))
    return str(path)


def _run(tmp_path, shapes, dbu=0.001):
    return run_drc(_write(tmp_path, shapes, dbu), "gf180mcu")


def _found(report, rule):
    return [v for v in report["violations"] if v["rule"] == rule]


def _device(implant, x, y=0, size=1000):
    return [
        (COMP, [x, y, x + size, y + size]),
        (implant, [x - PAD, y - PAD, x + size + PAD, y + size + PAD]),
    ]


def _nwell_layout(tap_x, active_x=0, *, tap=True):
    """PCOMP at ``active_x`` in a wide Nwell, NCOMP tap at ``tap_x``."""
    shapes = [(NWELL, [-5000, -5000, 80000, 6000])]
    shapes += _device(PPLUS, active_x)
    if tap:
        shapes += _device(NPLUS, tap_x)
    return shapes


def _psub_layout(tap_x, active_x=0, *, tap=True):
    """NCOMP outside any well at ``active_x``, PCOMP substrate tap at ``tap_x``."""
    shapes = _device(NPLUS, active_x)
    if tap:
        shapes += _device(PPLUS, tap_x)
    return shapes


LAYOUTS = [(NWELL_RULE, _nwell_layout), (PSUB_RULE, _psub_layout)]


@pytest.mark.parametrize("rule,build", LAYOUTS)
def test_zero_taps_with_active_present_is_a_violation(tmp_path, rule, build):
    report = _run(tmp_path, build(0, tap=False))
    found = _found(report, rule)
    assert len(found) == 1
    assert found[0]["check"] == "tap_distance"
    assert found[0]["bbox"] == {"left": 0, "bottom": 0, "right": 1000, "top": 1000}
    assert rule in report["coverage"]["rules_checked"]


@pytest.mark.parametrize("rule,build", LAYOUTS)
def test_tap_within_reach_is_clean(tmp_path, rule, build):
    report = _run(tmp_path, build(10000))
    assert _found(report, rule) == []
    assert rule in report["coverage"]["rules_checked"]


@pytest.mark.parametrize("rule,build", LAYOUTS)
def test_tap_beyond_reach_violates(tmp_path, rule, build):
    report = _run(tmp_path, build(30000))
    assert len(_found(report, rule)) == 1


@pytest.mark.parametrize("rule,build", LAYOUTS)
def test_axial_gap_boundary(tmp_path, rule, build):
    # Active spans x 0..1000; tap edge sits exactly D (then D+1 dbu) away.
    legal = _run(tmp_path, build(1000 + D))
    assert _found(legal, rule) == []
    over = _run(tmp_path, build(1000 + D + 1))
    assert len(_found(over, rule)) == 1


@pytest.mark.parametrize("rule,build", LAYOUTS)
def test_one_violation_per_selected_polygon(tmp_path, rule, build):
    shapes = build(0, tap=False)
    extra = _device(NPLUS if rule == PSUB_RULE else PPLUS, 5000)
    report = _run(tmp_path, shapes + extra)
    assert len(_found(report, rule)) == 2


def test_long_active_with_one_end_near_a_tap_is_legal(tmp_path):
    # Spike D1: whole-polygon selection -- a 40 um active touching reach is clean.
    shapes = [(NWELL, [-5000, -5000, 80000, 6000])]
    shapes += [
        (COMP, [0, 0, 40000, 1000]),
        (PPLUS, [-PAD, -PAD, 40000 + PAD, 1000 + PAD]),
    ]
    # tap sits 1.5 um past the active's right end (x = 41500..42500)
    shapes += _device(NPLUS, 41500)
    report = _run(tmp_path, shapes)
    assert _found(report, NWELL_RULE) == []


def test_dnwell_only_well_flags_the_pcomp(tmp_path):
    # Spike D2: DF.13 clips with raw `nwell`, so a DNWELL-only well gives no reach.
    shapes = [(DNWELL, [-5000, -5000, 80000, 6000])]
    shapes += _device(PPLUS, 0) + _device(NPLUS, 5000)
    report = _run(tmp_path, shapes)
    assert len(_found(report, NWELL_RULE)) == 1


def test_resistor_marker_over_the_only_tap_removes_it(tmp_path):
    shapes = _nwell_layout(5000)
    shapes.append((RES_MK, [4000, -1000, 7000, 2000]))
    report = _run(tmp_path, shapes)
    assert len(_found(report, NWELL_RULE)) == 1


@pytest.mark.parametrize("marker", [DUALGATE, V5_XTOR])
def test_lv_rule_ignores_active_touching_a_voltage_marker(tmp_path, marker):
    # Spike D3: marked active is out of the LV column (MV rules take it instead).
    shapes = _nwell_layout(0, tap=False) + [(marker, [-500, -500, 1500, 1500])]
    report = _run(tmp_path, shapes)
    assert _found(report, NWELL_RULE) == []
    assert NWELL_RULE not in report["coverage"]["rules_checked"]
    assert {
        "id": NWELL_RULE,
        "reason": "inapplicable_empty_checked_region",
    } in report["coverage"]["inapplicable"]


MV_LAYOUTS = [(NWELL_MV_RULE, _nwell_layout), (PSUB_MV_RULE, _psub_layout)]


def _under_dualgate(shapes):
    return shapes + [(DUALGATE, [-500, -500, 90000, 7000])]


@pytest.mark.parametrize("rule,build", MV_LAYOUTS)
def test_mv_zero_taps_inside_dualgate_is_a_violation(tmp_path, rule, build):
    report = _run(tmp_path, _under_dualgate(build(0, tap=False)))
    assert len(_found(report, rule)) == 1
    assert rule in report["coverage"]["rules_checked"]


@pytest.mark.parametrize("rule,build", MV_LAYOUTS)
def test_mv_axial_gap_boundary(tmp_path, rule, build):
    legal = _run(tmp_path, _under_dualgate(build(1000 + D_MV)))
    assert _found(legal, rule) == []
    over = _run(tmp_path, _under_dualgate(build(1000 + D_MV + 1)))
    assert len(_found(over, rule)) == 1


@pytest.mark.parametrize(
    "mv_rule,lv_rule,build",
    [
        (NWELL_MV_RULE, NWELL_RULE, _nwell_layout),
        (PSUB_MV_RULE, PSUB_RULE, _psub_layout),
    ],
)
def test_between_15_and_20_um_flags_mv_but_not_lv(tmp_path, mv_rule, lv_rule, build):
    gap = 1000 + 17000  # tap edge 17 um from the active
    mv = _run(tmp_path, _under_dualgate(build(gap)))
    assert len(_found(mv, mv_rule)) == 1
    assert _found(mv, lv_rule) == []
    lv = _run(tmp_path, build(gap))  # same geometry without Dualgate
    assert _found(lv, lv_rule) == []
    assert _found(lv, mv_rule) == []


@pytest.mark.parametrize("rule,build", MV_LAYOUTS)
def test_mv_rule_ignores_active_outside_dualgate(tmp_path, rule, build):
    report = _run(tmp_path, build(0, tap=False))
    assert _found(report, rule) == []


def test_mv_rules_do_not_trigger_wrong_column_warnings(tmp_path):
    report = _run(tmp_path, _under_dualgate(_nwell_layout(5000)))
    for warning in report["coverage"]["voltage_domain_warnings"]:
        assert NWELL_MV_RULE not in str(warning)
        assert PSUB_MV_RULE not in str(warning)


def test_voltage_markers_do_not_trigger_wrong_column_warnings(tmp_path):
    shapes = _nwell_layout(5000) + [(DUALGATE, [-500, -500, 1500, 1500])]
    report = _run(tmp_path, shapes)
    for warning in report["coverage"]["voltage_domain_warnings"]:
        assert NWELL_RULE not in str(warning)
        assert PSUB_RULE not in str(warning)


def test_comp_absent_is_inapplicable(tmp_path):
    report = _run(tmp_path, [(NPLUS, [0, 0, 1000, 1000])])
    cov = report["coverage"]
    for rule in (NWELL_RULE, PSUB_RULE):
        assert rule in cov["rules_skipped"] and rule not in cov["rules_checked"]
        assert {"id": rule, "reason": "no_applicable_geometry"} in cov["inapplicable"]


def test_no_active_is_inapplicable_empty_region(tmp_path):
    # COMP with no implant: no PCOMP/NCOMP at all, so nothing for DF.13/14 to check.
    report = _run(tmp_path, [(COMP, [0, 0, 1000, 1000])])
    cov = report["coverage"]
    for rule in (NWELL_RULE, PSUB_RULE):
        assert rule not in cov["rules_checked"]
        assert {"id": rule, "reason": "inapplicable_empty_checked_region"} in cov[
            "inapplicable"
        ]


def test_distance_not_representable_on_the_layout_dbu_is_skipped(tmp_path):
    # 20 um / 0.3 nm is not a whole number of database units.
    report = _run(tmp_path, _psub_layout(0, tap=False), dbu=0.0003)
    cov = report["coverage"]
    assert _found(report, PSUB_RULE) == []
    assert PSUB_RULE in cov["rules_skipped"]
    assert {"id": PSUB_RULE, "reason": "grid_not_representable"} in cov["skipped"]


def test_half_nanometre_dbu_is_representable(tmp_path):
    shapes = [
        (layer, [2 * c for c in box]) for layer, box in _psub_layout(0, tap=False)
    ]
    report = _run(tmp_path, shapes, dbu=0.0005)
    assert len(_found(report, PSUB_RULE)) == 1


def _spec(**overrides):
    layers = {
        name: (i, 0)
        for i, name in enumerate(
            ("nwell", "dnwell", "lvpwell", "nplus", "pplus", "res_mk"), start=1
        )
    }
    layers.update(dualgate=(7, 0), v5_xtor=(8, 0))
    params = {
        "well_domain": "inside",
        "voltage": "lv",
        "max_distance_um": 20.0,
        **layers,
    }
    params.update(overrides)
    return TapDistanceSpec(**params)


def test_valid_spec_constructs():
    assert _spec().step_um == 0.5


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"well_domain": "beside"}, "well_domain"),
        ({"voltage": "hv"}, "voltage"),
        ({"max_distance_um": 0}, "max_distance_um"),
        ({"max_distance_um": -1.0}, "max_distance_um"),
        ({"step_um": 0}, "step_um"),
        ({"max_distance_um": 20.3}, "multiple of step_um"),
    ],
)
def test_malformed_descriptor_raises_value_error(overrides, match):
    with pytest.raises(ValueError, match=match):
        _spec(**overrides)
