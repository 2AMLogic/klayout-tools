"""Tests for the ASAP7 ``VIA_*`` M1 landing-pad trim (issue #4495)."""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.place_and_route_via_pad import trim_via_pads

M1 = (19, 0)
V1 = (21, 0)
LEF_TO_GDS = {"M1": M1, "V1": V1}


def _layout(wire_boxes, pad=(-10, -10, 10, 10), at=(0, 0)):
    layout = kdb.Layout()
    layout.dbu = 0.001
    m1 = layout.layer(*M1)
    top = layout.create_cell("top")
    via = layout.create_cell("VIA_VIA12")
    via.shapes(m1).insert(kdb.Box(*pad))
    top.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(*at)))
    for box in wire_boxes:
        top.shapes(m1).insert(kdb.Box(*box))
    return layout, top, via, m1


def _with_cut(layout, via, cut=(-5, -5, 5, 5)):
    via.shapes(layout.layer(*V1)).insert(kdb.Box(*cut))


def _merged_m1(layout, top, m1):
    return kdb.Region(top.begin_shapes_rec(m1)).merged()


def _run(layout, top, variant="asap7"):
    return trim_via_pads(
        kdb, layout=layout, top_cell=top, variant=variant, lef_to_gds=LEF_TO_GDS
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
