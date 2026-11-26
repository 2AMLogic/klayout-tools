"""gf180mcu cut-enclosure rules opt into `require_containment` (issue #5915).

`DrcRule(require_containment=True)` (#2726) makes a cut-enclosure rule see a
*fully orphan* cut -- one entirely outside its conductor -- which the legacy
(#318) zero-overlap escape term, being `Region.interacting`-scoped, cannot.
This module pins, for gf180mcu:

* the single-population sides that are opted in (inside = clean,
  half-out = violates, fully-orphan = violates, conductor absent = violates);
* the negative half -- every multi-population side stays ``False`` and does
  not flag a cut that legitimately lands on the *other* population;
* the deck-wide enumeration, so a new cut rule cannot silently pick a side;
* the CO.4 operand -- `Contact` minus `SramCore` (issue #7954), checked by
  `comp.enclosing.contact.1` the way the foundry deck's `contact.not(sramcore)`
  does, plus the engine contract of `DrcRule.other_layer_subtract` itself.
"""

from __future__ import annotations

import pytest

from klayout_tools import decks
from klayout_tools.decks import DrcRule
from klayout_tools.decks import gf180mcu as gf
from klayout_tools.drc import DrcError, run_drc

kdb = pytest.importorskip("klayout.db")

CONTACT, VIA1, VIA2, VIA3, VIA4 = (33, 0), (35, 0), (38, 0), (40, 0), (41, 0)
POLY2, COMP = (30, 0), (22, 0)
M1, M2, M3, M4, M5 = (34, 0), (36, 0), (42, 0), (46, 0), (81, 0)
SRAMCORE = (108, 5)

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


# --- CO.4: Contact minus SramCore (issue #7954) -----------------------------
#
# The foundry CO.4 measures `contact.not(sramcore)` against Comp, escape term
# included, so a contact inside `SramCore` (108/5) is outside the rule. The
# curated rule passed the whole Contact layer and flagged every such contact.

_CO4 = "comp.enclosing.contact.1"

# The issue's 1 nm fixtures: Comp (22/0) from (0, 0) to (1000, 1000), Contact
# (33/0) from (10, 390) to (230, 610) -- 10 nm from Comp's left edge and far
# from every other edge. SramCore always spans y = -100 to 1100; its
# x-interval decides how much of the contact CO.4 still sees.
# (sramcore x-interval in nm, None when the layer is absent, expected CO.4
# marker count)
_CO4_SRAMCORE_CASES = [
    pytest.param(None, 1, id="absent"),
    pytest.param((-100, 1100), 0, id="masks-whole-contact"),
    pytest.param((-100, 100), 0, id="leaves-100nm-left-enclosure"),
    pytest.param((100, 1100), 1, id="leaves-10nm-left-enclosure"),
]


def _co4_shapes(sramcore_x):
    shapes = [(COMP, (0, 0, 1000, 1000)), (CONTACT, (10, 390, 230, 610))]
    if sramcore_x is not None:
        shapes.append((SRAMCORE, (sramcore_x[0], -100, sramcore_x[1], 1100)))
    return shapes


@pytest.mark.parametrize("sramcore_x,expected", _CO4_SRAMCORE_CASES)
def test_co4_operand_excludes_sramcore(tmp_path, sramcore_x, expected):
    report = run_drc(_layout(tmp_path, "co4.gds", _co4_shapes(sramcore_x)), "gf180mcu")

    assert len(_hits(report, _CO4)) == expected
    assert _CO4 in report["coverage"]["rules_checked"]


def test_co4_boundary_marker_sits_where_the_unmarked_one_does(tmp_path):
    """Masking the contact's right edge leaves its left 10 nm of enclosure
    short, so the retained marker is the unmarked control's own left-edge
    marker -- none at the SramCore cut edge."""

    def markers(sramcore_x):
        path = _layout(tmp_path, "co4.gds", _co4_shapes(sramcore_x))
        hits = _hits(run_drc(path, "gf180mcu"), _CO4)
        return [(hit["bbox"], hit["polygon"]) for hit in hits]

    control = markers(None)
    assert len(control) == 1
    assert markers((100, 1100)) == control


def test_co4_escape_term_sees_the_subtracted_operand(tmp_path):
    """The zero-overlap escape term runs on the same operand: a contact whose
    only escape past Comp lies inside SramCore no longer violates."""

    def hits(sramcore_x):
        shapes = [
            (COMP, (0, 0, 1000, 1000)),
            (CONTACT, (850, 390, 1070, 610)),  # 70 nm past Comp's right edge
        ]
        if sramcore_x is not None:
            shapes.append((SRAMCORE, (sramcore_x, -100, 1300, 1100)))
        return _hits(run_drc(_layout(tmp_path, "escape.gds", shapes), "gf180mcu"), _CO4)

    assert hits(None)  # unmasked: the escape and the 0 nm margin are reported
    # Masked from x = 930 on: the 70 nm left of the cut edge keep a 70 nm margin.
    assert hits(930) == []


def test_co4_reads_a_sramcore_drawn_in_a_child_cell(tmp_path):
    """The marker is read through the same recursive path as the contact: a
    SramCore drawn in a child still masks a contact drawn in the top cell."""
    off = (10_000, 10_000)
    marker = (-100 - off[0], -100 - off[1], 1100 - off[0], 1100 - off[1])
    top = [(COMP, (0, 0, 1000, 1000)), (CONTACT, (10, 390, 230, 610))]
    unmasked = _layout(tmp_path, "no_marker.gds", top)
    masked = _layout(
        tmp_path,
        "nested_marker.gds",
        [*top, (SRAMCORE, marker)],
        child=([SRAMCORE], off),
    )

    assert len(_hits(run_drc(unmasked, "gf180mcu"), _CO4)) == 1
    assert _hits(run_drc(masked, "gf180mcu"), _CO4) == []


def test_sramcore_is_a_named_input_of_co4(tmp_path):
    """SramCore is a layer the deck reads: it is named, listed in
    `deck_layers`, and never reported as "in the stream without rules"."""
    assert gf.LAYER_NAMES[SRAMCORE] == "SramCore"

    absent = run_drc(_layout(tmp_path, "a.gds", _co4_shapes(None)), "gf180mcu")
    drawn = run_drc(_layout(tmp_path, "d.gds", _co4_shapes((-100, 1100))), "gf180mcu")
    for coverage in (absent["coverage"], drawn["coverage"]):
        assert "108/5" in coverage["deck_layers"]
        assert "108/5" not in coverage["layers_in_stream_without_rules"]
        assert _CO4 in coverage["rules_checked"]
    # An absent marker is an empty region: nothing subtracted, nothing checked.
    assert "108/5" not in absent["coverage"]["layers_checked"]
    assert "108/5" in drawn["coverage"]["layers_checked"]


# --- engine contract of `DrcRule.other_layer_subtract` ----------------------


def _synthetic_deck(monkeypatch, rules):
    """Wire `run_drc(..., "synthetic")` to a one-off `rules` list."""
    monkeypatch.setattr("klayout_tools.drc.get_deck", lambda name: rules)
    monkeypatch.setattr("klayout_tools.drc.get_nominal_dbu", lambda name: 0.001)
    monkeypatch.setattr("klayout_tools.drc.get_layer_names", lambda name: {})


def test_other_layer_subtract_requires_other_layer(tmp_path, monkeypatch):
    """A subtraction with nothing to subtract from is a deck-authoring
    mistake: rejected with the rule id, never silently ignored."""
    _synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="syn.width.1",
                description="synthetic",
                layer=(1, 0),
                check="width",
                threshold_dbu=10,
                other_layer_subtract=((3, 0),),
            )
        ],
    )
    path = _layout(tmp_path, "syn.gds", [((1, 0), (0, 0, 1000, 1000))])

    with pytest.raises(DrcError, match="syn.width.1.*other_layer_subtract"):
        run_drc(path, "synthetic")


def test_other_layer_subtract_reaches_both_enclosing_terms(tmp_path, monkeypatch):
    """A synthetic zero-margin enclosing rule: the marker removes exactly the
    part of the cut that escapes, and only that part."""
    rule_id = "syn.enclosing.cut.1"
    _synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id=rule_id,
                description="synthetic",
                layer=(1, 0),  # conductor
                other_layer=(2, 0),  # cut
                check="enclosing",
                threshold_dbu=0,
                other_layer_subtract=((3, 0),),  # marker
            )
        ],
    )
    conductor, cut = ((1, 0), (0, 0, 1000, 1000)), ((2, 0), (900, 400, 1100, 600))

    def escape_boxes(marker_x):
        shapes = [conductor, cut]
        if marker_x is not None:
            shapes.append(((3, 0), (marker_x, 300, 1300, 700)))
        hits = _hits(
            run_drc(_layout(tmp_path, "syn.gds", shapes), "synthetic"), rule_id
        )
        return [hit["bbox"] for hit in hits]

    assert escape_boxes(None) == [
        {"left": 1000, "bottom": 400, "right": 1100, "top": 600}
    ]
    assert escape_boxes(1000) == []  # masks the whole 100 nm escape
    # A partial mask leaves a geometrically smaller escape, not the whole cut.
    assert escape_boxes(1050) == [
        {"left": 1000, "bottom": 400, "right": 1050, "top": 600}
    ]
