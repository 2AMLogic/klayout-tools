"""Tests for the ASAP7 ``VIA_*`` M1 landing-pad trim (issue #4495)."""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.place_and_route_via_pad import trim_via_pads

M1 = (19, 0)
LEF_TO_GDS = {"M1": M1}


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


def test_uncovered_pad_is_left_alone_and_reported():
    layout, top, via, m1 = _layout([(-50, -10, 0, 10)])
    report = _run(layout, top)
    assert report["status"] == "clean"
    assert report["pads_trimmed"] == 0 and report["pads_left"] == 1
    assert not via.shapes(m1).is_empty()


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
