"""Surviving pad-to-wire pairs from the public ASAP7 UCIe slice."""

import klayout.db as kdb
import pytest

from klayout_tools.place_and_route_via_pad import trim_via_pads


def _region(layout, cell, layer):
    return kdb.Region(cell.begin_shapes_rec(layout.layer(layer, 0))).merged()


def _repair(layout, top):
    return trim_via_pads(
        kdb, layout=layout, top_cell=top, variant="asap7A", lef_to_gds={"M1": (19, 0)}
    )


@pytest.mark.parametrize("rotation", [0, 1])
@pytest.mark.parametrize("crowded", [False, True])
def test_pad_faces_wire_with_another_via_further_along_its_side(rotation, crowded):
    # Translate the public slice's landing at (8028, 27432) nm to the origin.
    # Its facing 216 nm side shares 22 nm with a second pad 72 nm away.
    layout = kdb.Layout()
    layout.dbu = 0.001
    top, via = layout.create_cell("top"), layout.create_cell("VIA_VIA12")
    m1, v1 = layout.layer(19, 0), layout.layer(21, 0)
    via.shapes(m1).insert(kdb.Box(-9, -11, 9, 11))
    via.shapes(v1).insert(kdb.Box(-9, -9, 9, 9))
    turn = kdb.Trans(rotation, False, 0, 0)
    for x, y in ((0, 0), (36, -72)):
        top.insert(kdb.CellInstArray(via.cell_index(), turn * kdb.Trans(x, y)))
    for box in (kdb.Box(-18, -135, 0, 45), kdb.Box(27, -135, 45, 81)):
        top.shapes(m1).insert(box.transformed(turn))
    if crowded:
        # Extending up or down brings a separate wire within the spacing floor.
        for y in (-50, 32):
            top.shapes(m1).insert(kdb.Box(1, y, 25, y + 18).transformed(turn))
    before = _region(layout, top, 19)
    cuts = _region(layout, top, 21)
    master = _region(layout, via, 19)
    report = _repair(layout, top)
    after = _region(layout, top, 19)
    assert report["m1_landing_pad_wire_pairs_extended"] == int(not crowded)
    target = kdb.Box(9, -11, 27, 11).transformed(turn)
    s2 = [
        m
        for row in report["tip_check"]["rules"]
        if row["rule"] == "M1.S.2"
        for m in row["markers"]
    ]
    assert (
        any(kdb.DBox(*m["box_um"]).to_itype(layout.dbu) == target for m in s2)
        == crowded
    )
    if crowded:
        assert (after ^ before).is_empty()
    else:
        assert (before - after).is_empty() and after.count() == before.count()
        assert (after - before).interacting(cuts).is_empty()
    assert (_region(layout, top, 21) ^ cuts).is_empty()
    assert (_region(layout, via, 19) ^ master).is_empty()
    assert _repair(layout, top)["m1_landing_pad_wire_pairs_extended"] == 0
    assert (_region(layout, top, 19) ^ after).is_empty()


@pytest.mark.parametrize("rotation", [0, 1])
@pytest.mark.parametrize(
    "obstruction", [None, "cut", "passive", "label", "inset", "nested", "separate"]
)
def test_routed_notch_opposite_exposed_part_of_via(rotation, obstruction):
    # Relative to (3042, 34864) nm on the public slice, the 27 nm routed lip
    # faces just 9 nm of a placed pad across a 27 nm notch in the CLK metal.
    layout = kdb.Layout()
    layout.dbu = 0.001
    top, via = layout.create_cell("top"), layout.create_cell("VIA_VIA12")
    m1, v1 = layout.layer(19, 0), layout.layer(21, 0)
    via.shapes(m1).insert(kdb.Box(0, 0, 18, 22))
    inset = int(obstruction == "inset")
    via.shapes(v1).insert(kdb.Box(inset, 2, 18 - inset, 20))
    target = via
    if obstruction == "nested":
        target = layout.create_cell("wrapper")
        target.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans()))
    turn = kdb.Trans(rotation, False, 0, 0)
    top.insert(kdb.CellInstArray(target.cell_index(), turn * kdb.Trans(9, 81)))
    route = layout.create_cell("STANDARD_CELL")
    if obstruction == "separate":
        route.shapes(m1).insert(kdb.Box(0, 36, 18, 60))
        route.shapes(m1).insert(kdb.Box(0, 78, 18, 166))
    else:
        route.shapes(m1).insert(kdb.Box(0, 36, 18, 166))
    route.shapes(m1).insert(kdb.Box(0, 36, 45, 54))
    route.shapes(m1).insert(kdb.Box(27, 0, 45, 54))
    top.insert(kdb.CellInstArray(route.cell_index(), turn))
    if obstruction in ("cut", "passive"):
        index = layout.layer(18 if obstruction == "cut" else 112, 0)
        top.shapes(index).insert(kdb.Box(45, 40, 49, 47).transformed(turn))
    if obstruction == "label":
        top.shapes(layout.layer(19, 251)).insert(
            kdb.Text("OTHER", 46, 45).transformed(turn)
        )
    before, master, cuts = (
        _region(layout, top, 19),
        _region(layout, route, 19),
        _region(layout, top, 21),
    )
    report = _repair(layout, top)
    after = _region(layout, top, 19)
    assert report["m1_landing_pad_wire_pairs_extended"] == int(obstruction is None)
    s5 = [r for r in report["tip_check"]["rules"] if r["rule"] == "M1.S.5"]
    target_box = kdb.Box(18, 54, 27, 81).transformed(turn)
    assert any(
        kdb.DBox(*m["box_um"]).to_itype(layout.dbu) == target_box
        for r in s5
        for m in r["markers"]
    ) == (obstruction is not None)
    if obstruction is None:
        # The following V1 enclosure stage may independently add metal at the pad.
        wire_area = kdb.Region(kdb.Box(-20, -20, 80, 64).transformed(turn))
        extension = kdb.Region(kdb.Box(45, 36, 55, 54).transformed(turn))
        assert (((after - before) & wire_area) ^ extension).is_empty()
        assert (before - after).is_empty() and after.count() == before.count()
    else:
        assert (after ^ before).is_empty()
    assert (_region(layout, route, 19) ^ master).is_empty()
    assert (_region(layout, top, 21) ^ cuts).is_empty()
    assert _repair(layout, top)["m1_landing_pad_wire_pairs_extended"] == 0
    assert (_region(layout, top, 19) ^ after).is_empty()
