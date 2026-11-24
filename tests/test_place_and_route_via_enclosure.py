"""Placed ASAP7 V1s share a master, while each M1 repair belongs to one route."""

import klayout.db as kdb
import pytest

from klayout_tools.place_and_route_via_pad import trim_via_pads


def _region(cell, layer):
    return kdb.Region(cell.begin_shapes_rec(layer)).merged()


def _layout(rotation=0, mirror=False):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top, via = layout.create_cell("top"), layout.create_cell("VIA_VIA12")
    metal, cut = layout.layer(19, 0), layout.layer(21, 0)
    box = kdb.Box(0, 0, 18, 18)
    via.shapes(metal).insert(box)
    via.shapes(cut).insert(box)
    trans = kdb.Trans(rotation, mirror, 100, 200)
    top.insert(kdb.CellInstArray(via.cell_index(), trans))
    top.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(400, 0)))
    top.shapes(metal).insert(kdb.Box(-50, 0, 16, 18).transformed(trans))
    return layout, top, via, trans


def _repair(layout, top):
    report = trim_via_pads(
        kdb, layout=layout, top_cell=top, variant="asap7A", lef_to_gds={"M1": (19, 0)}
    )
    return report["m1_v1_enclosures_repaired"]


@pytest.mark.parametrize(
    ("rotation", "mirror"), [(0, False), (1, False), (2, True), (3, True)]
)
def test_placed_enclosure_preserves_master_and_bare_instance(rotation, mirror):
    layout, top, master, trans = _layout(rotation, mirror)
    metal, cut = layout.layer(19, 0), layout.layer(21, 0)
    before, cuts = _region(top, metal), _region(top, cut)
    pad = _region(master, metal)
    # Only the missing 2 nm end has room. The passive strips protect both
    # alternative sides and the point beyond the 2 nm end of the landing.
    for blocker in [
        kdb.Box(-1, -3, 19, -1),
        kdb.Box(-1, 19, 19, 21),
        kdb.Box(21, 4, 23, 14),
    ]:
        top.shapes(layout.layer(112, 0)).insert(blocker.transformed(trans))
    assert _repair(layout, top) == 1
    expected = kdb.Region(kdb.Box(18, 0, 20, 18).transformed(trans))
    assert ((_region(top, metal) - before) ^ expected).is_empty()
    assert (before - _region(top, metal)).is_empty()
    assert (_region(top, cut) ^ cuts).is_empty()
    assert (_region(master, metal) ^ pad).is_empty()
    assert _repair(layout, top) == 0


@pytest.mark.parametrize(
    "mode", ["legal", "M1 neighbor", "V0", "V1", "resistor", "capacitor", "label"]
)
def test_legal_and_obstructed_placements_keep_their_geometry(mode):
    layout, top, _, trans = _layout()
    local = [kdb.Box(19, 7, 20, 11), kdb.Box(7, -2, 11, -1), kdb.Box(7, 19, 11, 20)]
    if mode == "legal":
        top.shapes(layout.layer(19, 0)).insert(kdb.Box(3, 0, 20, 18).transformed(trans))
    elif mode == "label":
        for box in local:
            anchor = trans * box.center()
            top.shapes(layout.layer(19, 251)).insert(
                kdb.Text("other", anchor.x, anchor.y)
            )
    else:
        layer = {
            "M1 neighbor": 19,
            "V0": 18,
            "V1": 21,
            "resistor": 112,
            "capacitor": 113,
        }[mode]
        for box in local:
            top.shapes(layout.layer(layer, 0)).insert(box.transformed(trans))
    metal = layout.layer(19, 0)
    before = _region(top, metal)
    assert _repair(layout, top) == 0
    assert (_region(top, metal) ^ before).is_empty()


def test_notch_needs_enclosure_along_the_entire_side():
    layout, top, _, trans = _layout()
    metal = layout.layer(19, 0)
    # The right side already clears 2 nm except for a 1 nm wide notch.
    for box in [kdb.Box(18, 0, 20, 8), kdb.Box(18, 9, 20, 18)]:
        top.shapes(metal).insert(box.transformed(trans))
    before = _region(top, metal)
    assert _repair(layout, top) == 1
    assert (
        (_region(top, metal) - before)
        ^ kdb.Region(kdb.Box(18, 8, 20, 9).transformed(trans))
    ).is_empty()
