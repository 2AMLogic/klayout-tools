"""sg13g2 cut-containment regression pins (issue #5916, opt-in from #2726).

Every sg13g2 cut layer, both sides, with the population verdict that decides
whether ``require_containment`` (issue #2726) is set or the rule keeps the
legacy interaction-scoped zero-overlap escape term (#318):

* Cont (6/0) lower -- ``activ.enclosing.cont.1`` / ``gatpoly.enclosing.cont.1``:
  disjoint populations (Activ vs GatPoly) *and* a compound
  ``Activ | GatPoly`` region the engine cannot evaluate: both stay ``False``
  (the Cont-lower disclosure, out of #5916's scope).
* Cont (6/0) upper -- Metal1 only, single population: new curated
  ``metal1.enclosing.cont.1`` (threshold 0, containment). Applies to every
  Cont shape: contact bars land inside Metal1 like squares do.
* Via1 (19/0) lower/upper -- Metal1 / Metal2, single each: flip the existing
  ``metal1.enclosing.via1.1``; add curated ``metal2.enclosing.via1.1``.
* Via2 (29/0) lower/upper -- Metal2 / Metal3: flip ``metal2.enclosing.via2.1``;
  add curated ``metal3.enclosing.via2.1``.
* Via3 (49/0) lower/upper -- Metal3 / Metal4: flip ``metal3.enclosing.via3.1``;
  add curated ``metal4.enclosing.via3.1``.
* Via4 (66/0) lower/upper -- Metal4 / Metal5: flip ``metal4.enclosing.via4.1``;
  add curated ``metal5.enclosing.via4.1``.
* TopVia1 (125/0) lower -- Metal5 + MIM, but *single for containment*: MIM.c
  keeps every MIM plate >=0.6um inside Metal5 and MIM.d keeps a MIM-landing
  TopVia1 >=0.36um inside its plate, so both populations sit inside Metal5;
  ``metal5.enclosing.topvia1.1`` flips. ``mim.enclosing.topvia1.1`` itself
  stays ``False``: a routing TopVia1 lands on no MIM plate, so containment in
  MIM holds for one population only.
* TopVia1 (125/0) upper -- TopMetal1 only: ``topmetal1.enclosing.topvia1.1``
  flips.
* TopVia2 (133/0) both sides -- TopMetal1 / TopMetal2, single each: both flip.

The three-step reproduction pins, per flipped/curated rule: a cut fully
inside its conductor is clean; a cut hanging half out violates; a cut fully
orphaned from the conductor (zero overlap -- invisible to the #318 escape
term's ``.interacting`` pre-filter) violates. The negative half pins the
rules that must keep ``False`` and the populations that keep them so.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_deck
from klayout_tools.drc import run_drc

#: Fixed cut size per layer (dbu at the deck's nominal 0.001 um/dbu), so no
#: fixed-size width rule can substitute for the containment finding.
_CUT_SIZE = {
    (6, 0): 160,
    (19, 0): 190,
    (29, 0): 190,
    (49, 0): 190,
    (66, 0): 190,
    (125, 0): 420,
    (133, 0): 900,
}

#: (rule_id, conductor side layer, cut layer) for every flipped or curated
#: containment rule: the three-step reproduction runs against each.
_CUT_RULES = [
    ("metal1.enclosing.cont.1", (8, 0), (6, 0)),
    ("metal1.enclosing.via1.1", (8, 0), (19, 0)),
    ("metal2.enclosing.via1.1", (10, 0), (19, 0)),
    ("metal2.enclosing.via2.1", (10, 0), (29, 0)),
    ("metal3.enclosing.via2.1", (30, 0), (29, 0)),
    ("metal3.enclosing.via3.1", (30, 0), (49, 0)),
    ("metal4.enclosing.via3.1", (50, 0), (49, 0)),
    ("metal4.enclosing.via4.1", (50, 0), (66, 0)),
    ("metal5.enclosing.via4.1", (67, 0), (66, 0)),
    ("metal5.enclosing.topvia1.1", (67, 0), (125, 0)),
    ("topmetal1.enclosing.topvia1.1", (126, 0), (125, 0)),
    ("topmetal1.enclosing.topvia2.1", (126, 0), (133, 0)),
    ("topmetal2.enclosing.topvia2.1", (134, 0), (133, 0)),
]

#: Rules the issue curates from scratch: pure topology assertion precedent
#: (``sg13cmos5l.py``'s ``metal2.enclosing.via1.1``) -- threshold 0, no
#: provenance, scope marked ``(curated)``.
_CURATED_IDS = {
    "metal1.enclosing.cont.1",
    "metal2.enclosing.via1.1",
    "metal3.enclosing.via2.1",
    "metal4.enclosing.via3.1",
    "metal5.enclosing.via4.1",
}


#: Both landings per cut layer (Cont: its lower side is the uncovered
#: ``Activ | GatPoly`` compound, so only Metal1 is modelled here).
_LANDINGS = {
    (6, 0): ((8, 0),),
    (19, 0): ((8, 0), (10, 0)),
    (29, 0): ((10, 0), (30, 0)),
    (49, 0): ((30, 0), (50, 0)),
    (66, 0): ((50, 0), (67, 0)),
    (125, 0): ((67, 0), (126, 0)),
    (133, 0): ((126, 0), (134, 0)),
}


def draw_cut_landings(layout, top, cut_layer, skip, around_box):
    """Insert every conductor of ``cut_layer``'s landings except ``skip``,
    enclosing ``around_box`` generously, so the flipped containment rules
    stay quiet in fixtures that exercise a different rule in isolation."""
    for landing in _LANDINGS.get(cut_layer, ()):
        if landing != skip:
            top.shapes(layout.layer(*landing)).insert(around_box.enlarged(2500, 2500))


def _layout(tmp_path, name, shapes, dbu=0.001, extra_cell=None):
    """Write a GDS with ``shapes`` = ``[(layer, box), ...]`` in TOP (plus
    ``extra_cell`` shapes in a child cell instanced at the origin)."""
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    for layer, box in shapes:
        top.shapes(layout.layer(*layer)).insert(box)
    if extra_cell:
        child = layout.create_cell("CHILD")
        for layer, box in extra_cell:
            child.shapes(layout.layer(*layer)).insert(box)
        top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans()))
    path = tmp_path / name
    layout.write(str(path))
    return str(path)


def _hits(report, rule_id):
    return [v for v in report["violations"] if v["rule"] == rule_id]


def _place(conductor, cut_box, mode):
    """Conductor box for ``mode``: 'inside' (generous enclosure), 'half'
    (cut hangs half out), 'orphan' (conductor far away, zero overlap)."""
    size = cut_box.width()
    if mode == "inside":
        return cut_box.enlarged(size, size)
    if mode == "half":
        return kdb.Box(
            cut_box.left - size // 2,
            cut_box.bottom - size,
            cut_box.right - size // 2,
            cut_box.top + size,
        )
    return kdb.Box(size * 10, 0, size * 11, size)  # orphan


@pytest.mark.parametrize("rule_id,conductor,cut", _CUT_RULES)
@pytest.mark.parametrize("mode", ["inside", "half", "orphan"])
def test_sg13g2_cut_containment_three_step(tmp_path, rule_id, conductor, cut, mode):
    """Three-step reproduction per flipped/curated rule.

    'inside': cut fully inside its conductor -- clean. 'half': cut hangs
    half out -- violates. 'orphan': zero overlap with the conductor, which
    the legacy #318 escape term's ``.interacting`` pre-filter never reported
    -- the #5916 gap, now violated. The opposite landing always encloses the
    cut so only the rule under test can fire."""
    cut_box = kdb.Box(0, 0, _CUT_SIZE[cut], _CUT_SIZE[cut])
    shapes = [(cut, cut_box), (conductor, _place(conductor, cut_box, mode))]
    shapes.extend(
        (other, cut_box.enlarged(900, 900))
        for other in _LANDINGS[cut]
        if other != conductor
    )
    path = _layout(tmp_path, f"{mode}.gds", shapes)

    report = run_drc(path, "sg13g2")

    hits = _hits(report, rule_id)
    if mode == "inside":
        assert not hits
    else:
        assert hits and report["status"] == "violations"
        if mode == "orphan":
            # Containment reports the whole cut as `cut - conductor`.
            bbox = hits[0]["bbox"]
            assert (bbox["left"], bbox["bottom"], bbox["right"], bbox["top"]) == (
                0,
                0,
                _CUT_SIZE[cut],
                _CUT_SIZE[cut],
            )


def test_sg13g2_containment_opted_in_rules_exact_set():
    """Exactly the enumerated single-population sides opt in -- no more."""
    opted = {r.id for r in get_deck("sg13g2") if r.require_containment}
    assert opted == {rid for rid, _, _ in _CUT_RULES}


def test_sg13g2_curated_rules_follow_topology_assertion_precedent():
    """Each new rule: threshold 0, no provenance, scope ``(curated)``."""
    by_id = {r.id: r for r in get_deck("sg13g2")}
    for rule_id in sorted(_CURATED_IDS):
        rule = by_id[rule_id]
        assert rule.threshold_dbu == 0, rule_id
        assert rule.provenance is None, rule_id
        assert rule.scope.endswith("(curated)"), rule_id


def test_sg13g2_legacy_cut_rules_stay_false():
    """The multi-population sides keep ``require_containment=False``.

    Populations (module docstring table): Cont's lower side is the disjoint
    ``Activ | GatPoly`` pair (and a compound expression the engine cannot
    evaluate); ``mim.enclosing.topvia1.1``'s TopVia1 is shared between the
    MIM-cap population and ordinary Metal5->TopMetal1 routing."""
    by_id = {r.id: r for r in get_deck("sg13g2")}
    for rule_id in (
        "activ.enclosing.cont.1",
        "gatpoly.enclosing.cont.1",
        "mim.enclosing.topvia1.1",
    ):
        assert by_id[rule_id].require_containment is False, rule_id


def test_sg13g2_cont_lower_side_stays_interaction_scoped(tmp_path):
    """Cont orphan from both Activ and GatPoly: neither legacy rule fires.

    Flipping either would permanently violate every GatPoly-only (or
    Activ-only) contact -- exactly why the lower side stays uncovered."""
    cut = kdb.Box(0, 0, 160, 160)
    far = kdb.Box(50_000, 0, 60_000, 5_000)
    path = _layout(
        tmp_path,
        "legacy_cont.gds",
        [((6, 0), cut), ((1, 0), far), ((5, 0), far), ((8, 0), cut.enlarged(900, 900))],
    )
    report = run_drc(path, "sg13g2")
    assert not _hits(report, "activ.enclosing.cont.1")
    assert not _hits(report, "gatpoly.enclosing.cont.1")
    # The curated upper landing does contain the cut: quiet too.
    assert not _hits(report, "metal1.enclosing.cont.1")


def test_sg13g2_routing_topvia1_facing_no_mim_is_clean(tmp_path):
    """A TopVia1 inside Metal5 but orphan from MIM stays quiet.

    ``mim.enclosing.topvia1.1`` keeps the legacy semantics (#5916): required
    containment in MIM would flag every routing via."""
    cut = kdb.Box(0, 0, 420, 420)
    path = _layout(
        tmp_path,
        "routing_tv1.gds",
        [
            ((125, 0), cut),
            ((67, 0), cut.enlarged(900, 900)),
            ((126, 0), cut.enlarged(900, 900)),
            ((36, 0), kdb.Box(50_000, 0, 60_000, 5_000)),
        ],
    )
    report = run_drc(path, "sg13g2")
    assert not _hits(report, "mim.enclosing.topvia1.1")
    assert not _hits(report, "metal5.enclosing.topvia1.1")
    assert not _hits(report, "topmetal1.enclosing.topvia1.1")


def test_sg13g2_conductor_absent_from_stream_reports_every_cut(tmp_path):
    """Metal1 never drawn: ``metal1.enclosing.via1.1`` is checked against an
    empty conductor, so every Via1 is reported under it."""
    cut = kdb.Box(0, 0, 190, 190)
    path = _layout(
        tmp_path,
        "no_m1.gds",
        [((19, 0), cut), ((10, 0), cut.enlarged(900, 900))],
    )
    report = run_drc(path, "sg13g2")
    assert len(_hits(report, "metal1.enclosing.via1.1")) == 1


def test_sg13g2_cut_coincident_with_conductor_boundary_is_clean(tmp_path):
    """Exact boundary coincidence passes containment (threshold 0)."""
    cut = kdb.Box(0, 0, 160, 160)
    path = _layout(
        tmp_path,
        "exact.gds",
        [((6, 0), cut), ((8, 0), kdb.Box(0, 0, 160, 160))],
    )
    report = run_drc(path, "sg13g2")
    assert not _hits(report, "metal1.enclosing.cont.1")


def test_sg13g2_cut_in_child_cell_containment_sees_hierarchy(tmp_path):
    """A cut drawn in a child cell is merged into the checked region: the
    containment rule sees it wherever the conductor lives."""
    cut = kdb.Box(0, 0, 190, 190)
    path = _layout(
        tmp_path,
        "hier.gds",
        [((8, 0), cut.enlarged(900, 900)), ((10, 0), cut.enlarged(900, 900))],
        extra_cell=[((19, 0), cut)],
    )
    report = run_drc(path, "sg13g2")
    assert not _hits(report, "metal1.enclosing.via1.1")
    path = _layout(
        tmp_path,
        "hier_orphan.gds",
        [
            ((8, 0), kdb.Box(50_000, 0, 60_000, 5_000)),
            ((10, 0), cut.enlarged(900, 900)),
        ],
        extra_cell=[((19, 0), cut)],
    )
    report = run_drc(path, "sg13g2")
    assert _hits(report, "metal1.enclosing.via1.1")


def test_sg13g2_containment_at_non_1nm_dbu(tmp_path):
    """5 nm input dbu: same physical geometry, same containment verdicts."""
    dbu = 0.005
    cut = kdb.Box(0, 0, 38, 38)  # 0.19 um Cont-free Via1 at 5 nm dbu
    clean = _layout(
        tmp_path,
        "dbu_ok.gds",
        [
            ((19, 0), cut),
            ((8, 0), cut.enlarged(180, 180)),
            ((10, 0), cut.enlarged(180, 180)),
        ],
        dbu=dbu,
    )
    assert not _hits(run_drc(clean, "sg13g2"), "metal1.enclosing.via1.1")
    orphan = _layout(
        tmp_path,
        "dbu_orphan.gds",
        [
            ((19, 0), cut),
            ((8, 0), kdb.Box(10_000, 0, 12_000, 1_000)),
            ((10, 0), cut.enlarged(180, 180)),
        ],
        dbu=dbu,
    )
    assert _hits(run_drc(orphan, "sg13g2"), "metal1.enclosing.via1.1")
