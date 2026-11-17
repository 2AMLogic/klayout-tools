"""gf180mcu cut-enclosure rules opt into `require_containment` (issue #5915).

`DrcRule(require_containment=True)` (#2726) makes a cut-enclosure rule see a
*fully orphan* cut -- one entirely outside its conductor -- which the legacy
(#318) zero-overlap escape term, being `Region.interacting`-scoped, cannot.
This module pins, for gf180mcu:

* the single-population sides that are opted in (inside = clean,
  half-out = violates, fully-orphan = violates, conductor absent = violates);
* the negative half -- every multi-population side stays ``False`` and does
  not flag a cut that legitimately lands on the *other* population;
* the deck-wide enumeration, so a new cut rule cannot silently pick a side.
"""

from __future__ import annotations

import pytest

from klayout_tools import decks
from klayout_tools.decks import gf180mcu as gf
from klayout_tools.drc import run_drc

kdb = pytest.importorskip("klayout.db")

CONTACT, VIA1, VIA2, VIA3, VIA4 = (33, 0), (35, 0), (38, 0), (40, 0), (41, 0)
POLY2, COMP = (30, 0), (22, 0)
M1, M2, M3, M4, M5 = (34, 0), (36, 0), (42, 0), (46, 0), (81, 0)

# (rule id, cut, conductor under test, the cut's *other* landing, cut size nm).
# Cut sizes are the fixed-size rule values (CO.1 0.22um, Vn.1 0.26um) so no
# width defect can stand in for the containment finding.
_OPTED_IN = [
    ("metal1.enclosing.contact.1", CONTACT, M1, None, 220),
    ("metal1.enclosing.via1.1", VIA1, M1, M2, 260),
    ("metal2.enclosing.via1.1", VIA1, M2, M1, 260),
    ("metal2.enclosing.via2.1", VIA2, M2, M3, 260),
    ("metal3.enclosing.via2.1", VIA2, M3, M2, 260),
    ("metal3.enclosing.via3.1", VIA3, M3, M4, 260),
    ("metal4.enclosing.via3.1", VIA3, M4, M3, 260),
    ("metal5.enclosing.via4.1", VIA4, M5, None, 260),
]

# Multi-population sides: the cut lands on one of several disjoint
# populations, so a single-layer containment requirement would false-positive.
_STAYS_FALSE = [
    "poly2.enclosing.contact.1",  # contact lands on poly2 *or* comp
    "comp.enclosing.contact.1",
    "metal4.enclosing.via4.1",  # a MiM via4 lands on mim, not metal4
    "mim.enclosing.via4.1",
]

_NON_CUT_CONTAINMENT = {"ldmos_xtor.enclosing.mvpsd.1"}  # pre-existing, not a cut


def _layout(tmp_path, name, shapes, *, dbu=0.001, child=None):
    """Write a GDS of ``(layer, (x0, y0, x1, y1))`` boxes in nm.

    ``child`` = ``(layers_in_child, offset_nm)`` puts those layers' shapes in a
    child cell instantiated at ``offset_nm`` (nested-cell case).
    """
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB") if child else None
    child_layers = child[0] if child else ()
    scale = 0.001 / dbu
    for layer, box in shapes:
        target = sub if layer in child_layers else top
        target.shapes(layout.layer(*layer)).insert(
            kdb.Box(*[round(v * scale) for v in box])
        )
    if child:
        ox, oy = child[1]
        top.insert(
            kdb.CellInstArray(
                sub.cell_index(), kdb.Trans(round(ox * scale), round(oy * scale))
            )
        )
    path = tmp_path / name
    layout.write(str(path))
    return str(path)


def _hits(report, rule_id):
    return [v for v in report["violations"] if v["rule"] == rule_id]


def _scenario(kind, cut, conductor, other, size):
    cut_box = (0, 0, size, size)
    pad = 900
    good = (-pad, -pad, size + pad, size + pad)
    shapes = [(cut, cut_box)]
    if other is not None:
        shapes.append((other, good))
    if kind == "inside":
        shapes.append((conductor, good))
    elif kind == "half_out":  # covers the left half only; the right half is bare
        shapes.append((conductor, (-pad, -pad, size // 2, size + pad)))
    elif kind == "orphan":  # drawn, but nowhere near the cut
        shapes.append((conductor, (50_000, 0, 51_000, 1_000)))
    elif kind != "absent":  # "absent": conductor layer not in the stream at all
        raise AssertionError(kind)
    return shapes


@pytest.mark.parametrize("rule_id,cut,conductor,other,size", _OPTED_IN)
def test_opted_in_rules_match_the_deck(rule_id, cut, conductor, other, size):
    rule = next(r for r in decks.get_deck("gf180mcu") if r.id == rule_id)
    assert rule.require_containment is True
    assert (rule.layer, rule.other_layer) == (conductor, cut)


@pytest.mark.parametrize("rule_id,cut,conductor,other,size", _OPTED_IN)
def test_inside_is_clean(tmp_path, rule_id, cut, conductor, other, size):
    path = _layout(
        tmp_path, "inside.gds", _scenario("inside", cut, conductor, other, size)
    )
    assert _hits(run_drc(path, "gf180mcu"), rule_id) == []


@pytest.mark.parametrize("kind", ["half_out", "orphan", "absent"])
@pytest.mark.parametrize("rule_id,cut,conductor,other,size", _OPTED_IN)
def test_unlanded_cut_violates(tmp_path, kind, rule_id, cut, conductor, other, size):
    path = _layout(
        tmp_path, f"{kind}.gds", _scenario(kind, cut, conductor, other, size)
    )
    report = run_drc(path, "gf180mcu")
    hits = _hits(report, rule_id)
    assert hits and report["status"] == "violations", (kind, rule_id)
    assert rule_id in report["coverage"]["rules_checked"]
    if kind != "half_out":  # fully orphan: the report is the whole cut
        bbox = hits[0]["bbox"]
        assert (bbox["left"], bbox["bottom"], bbox["right"], bbox["top"]) == (
            0,
            0,
            size,
            size,
        )


@pytest.mark.parametrize("rule_id", [r[0] for r in _OPTED_IN])
def test_scope_comment_recorded_per_rule(rule_id):
    """Each flip carries its one-line reason in the deck source."""
    import inspect

    src = inspect.getsource(gf)
    start = src.index(f'id="{rule_id}"')
    window = src[
        start : src.index("DrcRule(", start + 1)
        if "DrcRule(" in src[start + 1 :]
        else None
    ]
    assert "require_containment=True,  # #5915" in window


@pytest.mark.parametrize("rule_id", _STAYS_FALSE)
def test_multi_population_sides_stay_false(rule_id):
    rule = next(r for r in decks.get_deck("gf180mcu") if r.id == rule_id)
    assert rule.require_containment is False


def test_enumeration_is_exactly_the_opted_in_set():
    """Every gf180mcu enclosing/enclosed rule is either opted in (listed above)
    or a named multi-population exception -- nothing is silently unclassified."""
    rules = [
        r for r in decks.get_deck("gf180mcu") if r.check in ("enclosing", "enclosed")
    ]
    opted = {r.id for r in rules if r.require_containment}
    assert opted == {r[0] for r in _OPTED_IN} | _NON_CUT_CONTAINMENT
    for rid in _STAYS_FALSE:
        assert rid in {r.id for r in rules}


def test_contact_on_poly2_only_does_not_flag_comp_rule(tmp_path):
    shapes = [
        (CONTACT, (0, 0, 220, 220)),
        (M1, (-900, -900, 1120, 1120)),
        (POLY2, (-900, -900, 1120, 1120)),
    ]
    report = run_drc(_layout(tmp_path, "poly.gds", shapes), "gf180mcu")
    assert _hits(report, "comp.enclosing.contact.1") == []
    assert _hits(report, "poly2.enclosing.contact.1") == []


def test_contact_on_comp_only_does_not_flag_poly2_rule(tmp_path):
    shapes = [
        (CONTACT, (0, 0, 220, 220)),
        (M1, (-900, -900, 1120, 1120)),
        (COMP, (-900, -900, 1120, 1120)),
    ]
    report = run_drc(_layout(tmp_path, "comp.gds", shapes), "gf180mcu")
    assert _hits(report, "poly2.enclosing.contact.1") == []
    assert _hits(report, "comp.enclosing.contact.1") == []


def test_via4_on_metal5_only_does_not_flag_metal4_rule(tmp_path):
    """A routing Via4 (Metal5 landing) with no Metal4 drawn: the multi-population
    ``metal4.enclosing.via4.1`` must stay quiet."""
    shapes = [(VIA4, (0, 0, 260, 260)), (M5, (-900, -900, 1160, 1160))]
    report = run_drc(_layout(tmp_path, "v4.gds", shapes), "gf180mcu")
    assert _hits(report, "metal4.enclosing.via4.1") == []
    assert _hits(report, "metal5.enclosing.via4.1") == []


# --- edge cases -------------------------------------------------------------


def test_cut_coincident_with_conductor_boundary_passes(tmp_path):
    """Zero-margin rule (`metal1.enclosing.via1.1`, V1.3a): a cut whose edge
    sits exactly on the conductor boundary is contained, so it passes."""
    shapes = [
        (VIA1, (0, 0, 260, 260)),
        (M1, (0, 0, 1000, 1000)),
        (M2, (-900, -900, 1160, 1160)),
    ]
    report = run_drc(_layout(tmp_path, "edge.gds", shapes), "gf180mcu")
    assert _hits(report, "metal1.enclosing.via1.1") == []


@pytest.mark.parametrize(
    "rule_id,cut,conductor,other,size", _OPTED_IN[:2] + _OPTED_IN[-1:]
)
def test_cut_inside_a_hole_fails(tmp_path, rule_id, cut, conductor, other, size):
    ring = kdb.Region(kdb.Box(-1500, -1500, size + 1500, size + 1500)) - kdb.Region(
        kdb.Box(-300, -300, size + 300, size + 300)
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(*cut)).insert(kdb.Box(0, 0, size, size))
    top.shapes(layout.layer(*conductor)).insert(ring)
    if other is not None:
        top.shapes(layout.layer(*other)).insert(
            kdb.Box(-900, -900, size + 900, size + 900)
        )
    path = tmp_path / "hole.gds"
    layout.write(str(path))
    assert _hits(run_drc(str(path), "gf180mcu"), rule_id)


@pytest.mark.parametrize(
    "rule_id,cut,conductor,other,size", _OPTED_IN[:1] + _OPTED_IN[-1:]
)
def test_nested_cell_cut(tmp_path, rule_id, cut, conductor, other, size):
    off = (10_000, 10_000)
    good = (-900 + off[0], -900 + off[1], size + 900 + off[0], size + 900 + off[1])
    cut_box = (0, 0, size, size)
    # clean: cut in the child cell, landing in TOP around the instance.
    clean = _layout(
        tmp_path,
        "nest_clean.gds",
        [(cut, cut_box), (conductor, good)],
        child=([cut], off),
    )
    assert _hits(run_drc(clean, "gf180mcu"), rule_id) == []
    # orphan: landing drawn elsewhere in TOP.
    far = (50_000, 0, 51_000, 1_000)
    orphan = _layout(
        tmp_path,
        "nest_orphan.gds",
        [(cut, cut_box), (conductor, far)],
        child=([cut], off),
    )
    hits = _hits(run_drc(orphan, "gf180mcu"), rule_id)
    assert hits
    bbox = hits[0]["bbox"]
    assert (bbox["left"], bbox["bottom"]) == off


@pytest.mark.parametrize(
    "rule_id,cut,conductor,other,size", _OPTED_IN[:1] + _OPTED_IN[-1:]
)
def test_non_1nm_dbu(tmp_path, rule_id, cut, conductor, other, size):
    inside = _layout(
        tmp_path,
        "d2_in.gds",
        _scenario("inside", cut, conductor, other, size),
        dbu=0.002,
    )
    assert _hits(run_drc(inside, "gf180mcu"), rule_id) == []
    orphan = _layout(
        tmp_path,
        "d2_orph.gds",
        _scenario("orphan", cut, conductor, other, size),
        dbu=0.002,
    )
    assert _hits(run_drc(orphan, "gf180mcu"), rule_id)
