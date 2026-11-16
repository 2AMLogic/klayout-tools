"""Tests for the ASAP7 ``VIA_*`` metal landing-pad trim (issue #4495)."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.place_and_route_via_pad import trim_via_pads

M1 = (19, 0)
M2 = (20, 0)
M3 = (30, 0)
M4 = (40, 0)
V1 = (21, 0)
V2 = (25, 0)
V3 = (35, 0)
V4 = (45, 0)
LEF_TO_GDS = {"M1": M1, "V1": V1}
#: The full ASAP7 routed-net map a real run resolves. Used by the
#: multi-layer tests; the single-layer ones keep the narrower map above so
#: they still exercise the "only one tip-rule layer is mapped" path.
FULL_LEF_TO_GDS = {
    "M1": M1,
    "M2": M2,
    "M3": M3,
    "M4": M4,
    "V1": V1,
    "V2": V2,
    "V3": V3,
    "V4": V4,
}


def _layout(wire_boxes, pad=(-10, -10, 10, 10), at=(0, 0), layer=M1):
    layout = kdb.Layout()
    layout.dbu = 0.001
    metal = layout.layer(*layer)
    top = layout.create_cell("top")
    via = layout.create_cell("VIA_VIA12")
    via.shapes(metal).insert(kdb.Box(*pad))
    top.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(*at)))
    for box in wire_boxes:
        top.shapes(metal).insert(kdb.Box(*box))
    return layout, top, via, metal


def _with_cut(layout, via, cut=(-5, -5, 5, 5), layer=V1):
    via.shapes(layout.layer(*layer)).insert(kdb.Box(*cut))


def _merged_m1(layout, top, m1):
    return kdb.Region(top.begin_shapes_rec(m1)).merged()


def _run(layout, top, variant="asap7", lef_to_gds=None):
    return trim_via_pads(
        kdb,
        layout=layout,
        top_cell=top,
        variant=variant,
        lef_to_gds=LEF_TO_GDS if lef_to_gds is None else lef_to_gds,
    )


def test_covered_pad_is_trimmed():
    layout, top, via, m1 = _layout([(-50, -10, 50, 10)])
    report = _run(layout, top)
    assert report["status"] == "trimmed"
    assert report["pads_trimmed"] == 1 and report["cells_touched"] == 1
    assert via.shapes(m1).is_empty()


def test_uncovered_pad_without_v1_is_left_alone_and_reported():
    layout, top, via, m1 = _layout([(-50, -10, 0, 10)])
    report = _run(layout, top)
    assert report["status"] == "clean"
    assert report["pads_trimmed"] == 0 and report["pads_left"] == 1
    assert not via.shapes(m1).is_empty()


def test_protruding_pad_is_shrunk_and_merged_m1_changes():
    layout, top, via, m1 = _layout([(-50, -10, 0, 10)], pad=(-20, -20, 20, 20))
    _with_cut(layout, via)
    before = _merged_m1(layout, top, m1)
    report = _run(layout, top)
    after = _merged_m1(layout, top, m1)
    assert report["status"] == "trimmed" and report["pads_shrunk"] == 1
    assert report["pads_trimmed"] == 0 and report["pads_left"] == 0
    assert after != before
    # Pad cut back to the V1 cut grown by 5 dbu; still merged with the wire.
    assert kdb.Region(via.shapes(m1)).bbox() == kdb.Box(-10, -10, 10, 10)
    assert after.count() == 1
    # V1 enclosure of 5 dbu on every side survives in the merged M1.
    assert (kdb.Region(kdb.Box(-10, -10, 10, 10)) - after).is_empty()


def test_pad_protruding_one_side_is_cut_back_on_that_side_only():
    layout, top, via, m1 = _layout([(-50, -4, 50, 4)], pad=(-10, -10, 30, 10))
    _with_cut(layout, via)
    report = _run(layout, top)
    assert report["pads_shrunk"] == 1
    assert kdb.Region(via.shapes(m1)).bbox() == kdb.Box(-10, -10, 10, 10)


def test_shrink_that_would_disconnect_pad_from_wire_is_refused():
    # Wire only touches the far end of the pad, beyond cut + enclosure.
    layout, top, via, m1 = _layout([(20, -10, 60, 10)], pad=(-10, -10, 30, 10))
    _with_cut(layout, via)
    report = _run(layout, top)
    assert report["pads_shrunk"] == 0 and report["pads_left"] == 1
    assert kdb.Region(via.shapes(m1)).bbox() == kdb.Box(-10, -10, 30, 10)


def test_pad_covered_in_only_one_of_two_instances_is_kept():
    layout, top, via, m1 = _layout([(-50, -10, 50, 10)])
    top.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(1000, 0)))
    report = _run(layout, top)
    assert report["pads_trimmed"] == 0 and report["pads_left"] == 1
    assert not via.shapes(m1).is_empty()


def test_other_via_pad_does_not_count_as_wire():
    layout, top, via, m1 = _layout([])
    other = layout.create_cell("VIA_OTHER")
    other.shapes(m1).insert(kdb.Box(-50, -10, 50, 10))
    top.insert(kdb.CellInstArray(other.cell_index(), kdb.Trans(0, 0)))
    report = _run(layout, top)
    assert report["pads_trimmed"] == 0


def test_non_asap7_is_untouched_noop():
    layout, top, via, m1 = _layout([(-50, -10, 50, 10)])
    report = _run(layout, top, variant="sky130A")
    assert report["status"] == "skipped"
    assert not via.shapes(m1).is_empty()


def test_no_via_cells_is_clean():
    layout = kdb.Layout()
    layout.layer(*M1)
    top = layout.create_cell("top")
    assert _run(layout, top)["status"] == "clean"


# --------------------------------------------------------------------------
# Every tip-rule layer, not only M1 (issue #4495's follow-up report)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metal", "cut", "name"),
    [(M1, V1, "M1"), (M2, V2, "M2"), (M3, V3, "M3")],
    ids=["M1-over-V1", "M2-over-V2", "M3-over-V3"],
)
def test_covered_pad_is_trimmed_on_every_tip_rule_layer(metal, cut, name):
    """The deck states the tip family for M1 and applies it to M2/M3, so a
    pad redundant with routed wire is dropped on each of the three -- the
    pass must not be scoped to the V1/M1 landing pad."""
    layout, top, via, index = _layout([(-50, -10, 50, 10)], layer=metal)
    _with_cut(layout, via, layer=cut)
    report = _run(layout, top, lef_to_gds=FULL_LEF_TO_GDS)
    assert report["status"] == "trimmed"
    assert report["pads_trimmed"] == 1 and report["cells_touched"] == 1
    assert via.shapes(index).is_empty()
    assert [entry for entry in report["layers"] if entry["pads_trimmed"]] == [
        {
            "layer_name": name,
            "layer": list(metal),
            "pads_trimmed": 1,
            "pads_shrunk": 0,
            "pads_left": 0,
        }
    ]


@pytest.mark.parametrize(
    ("metal", "cut"),
    [(M2, V2), (M3, V3)],
    ids=["M2-over-V2", "M3-over-V3"],
)
def test_protruding_pad_is_shrunk_on_the_upper_tip_rule_layers(metal, cut):
    """Same 5 nm cut enclosure as M1: the deck requires 5 nm of every
    (V1-V3, M1-M3) pair, so the shrink is correct across the whole span."""
    layout, top, via, index = _layout(
        [(-50, -10, 0, 10)], pad=(-20, -20, 20, 20), layer=metal
    )
    _with_cut(layout, via, layer=cut)
    report = _run(layout, top, lef_to_gds=FULL_LEF_TO_GDS)
    assert report["pads_shrunk"] == 1 and report["pads_trimmed"] == 0
    assert kdb.Region(via.shapes(index)).bbox() == kdb.Box(-10, -10, 10, 10)


def test_m4_pad_is_left_alone_even_when_fully_covered():
    """M4 is outside the span: ``V3.M4.EN.2`` is 11 nm (not 5 nm) and the
    deck's spacing there is unconditional, so this pass must not edit M4 --
    the layer is not even reported as checked."""
    layout, top, via, index = _layout([(-50, -10, 50, 10)], layer=M4)
    _with_cut(layout, via, layer=V4)
    report = _run(layout, top, lef_to_gds=FULL_LEF_TO_GDS)
    assert report["status"] == "clean"
    assert report["pads_trimmed"] == 0 and report["cells_touched"] == 0
    assert not via.shapes(index).is_empty()
    # No tip-rule layer is drawn in this layout at all, so none was checked.
    assert report["layers"] == []


def test_pads_on_two_layers_of_one_via_cell_are_both_trimmed_once():
    """A real ``VIA_VIA12`` carries a pad on each of the two metals it joins.
    Both are dropped, and the cell is counted once in ``cells_touched``."""
    layout, top, via, m1_index = _layout([(-50, -10, 50, 10)], layer=M1)
    m2_index = layout.layer(*M2)
    via.shapes(m2_index).insert(kdb.Box(-10, -10, 10, 10))
    top.shapes(m2_index).insert(kdb.Box(-50, -10, 50, 10))
    report = _run(layout, top, lef_to_gds=FULL_LEF_TO_GDS)
    assert report["pads_trimmed"] == 2 and report["cells_touched"] == 1
    assert via.shapes(m1_index).is_empty() and via.shapes(m2_index).is_empty()
    assert {
        entry["layer_name"]: entry["pads_trimmed"] for entry in report["layers"]
    } == {
        "M1": 1,
        "M2": 1,
    }


def test_layer_absent_from_the_map_is_not_reported_as_checked():
    layout, top, _, _ = _layout([(-50, -10, 50, 10)])
    report = _run(layout, top)
    assert [entry["layer_name"] for entry in report["layers"]] == ["M1"]


def test_no_tip_rule_layer_in_the_map_is_a_named_skip():
    layout, top, _, _ = _layout([(-50, -10, 50, 10)])
    report = _run(layout, top, lef_to_gds={"V1": V1})
    assert report["status"] == "skipped"
    assert "no M1/M2/M3 routed-net layer" in report["reason"]
