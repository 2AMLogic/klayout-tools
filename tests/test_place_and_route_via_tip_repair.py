"""Placed V1 landings and control geometries for the ASAP7 M1 repair."""

import klayout.db as kdb
import pytest

from klayout_tools.place_and_route_via_pad import trim_via_pads
from klayout_tools.place_and_route_via_tip import check_via_pad_tips


def _pair(cell_name="VIA_VIA12", rotation=0):
    layout = kdb.Layout()
    layout.dbu = 0.001
    m1, v1 = layout.layer(19, 0), layout.layer(21, 0)
    top, via = layout.create_cell("top"), layout.create_cell(cell_name)
    via.shapes(m1).insert(kdb.Box(-9, -11, 9, 11))
    via.shapes(v1).insert(kdb.Box(-9, -9, 9, 9))
    transform = kdb.Trans(rotation, False, 0, 0)
    for x in (0, 36, 400):
        top.insert(kdb.CellInstArray(via.cell_index(), transform * kdb.Trans(x, 0)))
    return layout, top, via


def _region(layout, cell, layer):
    return kdb.Region(cell.begin_shapes_rec(layout.layer(layer, 0))).merged()


def _repair(layout, top):
    return trim_via_pads(
        kdb, layout=layout, top_cell=top, variant="asap7A", lef_to_gds={"M1": (19, 0)}
    )


@pytest.mark.parametrize("rotation", [0, 1])
def test_repair_extends_only_the_two_placed_landings(rotation):
    layout, top, master = _pair(rotation=rotation)
    before, cuts = _region(layout, top, 19), _region(layout, top, 21)
    original = _region(layout, master, 19)
    assert (
        check_via_pad_tips(
            kdb,
            layout=layout,
            top_cell=top,
            variant="asap7A",
            lef_to_gds={"M1": (19, 0)},
        )["rules"][0]["rule"]
        == "M1.S.4"
    )
    report = _repair(layout, top)
    assert report["tip_check"]["violations"] == 0
    assert report["m1_landing_pairs_extended"] == 1
    after = _region(layout, top, 19)
    assert after.count() == before.count() == 3
    assert (before - after).is_empty()
    assert (after - before).area() == 2 * (37 - 22) * 18
    assert (_region(layout, master, 19) ^ original).is_empty()
    assert (_region(layout, top, 21) ^ cuts).is_empty()
    assert _repair(layout, top)["m1_landing_pairs_extended"] == 0


@pytest.mark.parametrize("side", [-1, 1])
def test_growth_avoids_contact_on_one_side(side):
    layout, top, _ = _pair()
    # A V0 just outside the original M1 cannot be connected by the edit.
    y = side * 14
    top.shapes(layout.layer(18, 0)).insert(kdb.Box(-4, y - 1, 4, y + 1))
    before = _region(layout, top, 19)
    assert _repair(layout, top)["m1_landing_pairs_extended"] == 1
    added = _region(layout, top, 19) - before
    assert added.interacting(_region(layout, top, 18)).is_empty()
    assert (added.bbox().top if side > 0 else -added.bbox().bottom) == -11


def test_obstructed_landing_keeps_diagnostic():
    layout, top, _ = _pair()
    for y in (-14, 14):
        top.shapes(layout.layer(18, 0)).insert(kdb.Box(-4, y - 1, 4, y + 1))
    before = _region(layout, top, 19)
    report = _repair(layout, top)
    assert report["m1_landing_pairs_extended"] == 0
    assert report["tip_check"]["rules"][0]["rule"] == "M1.S.4"
    assert (_region(layout, top, 19) ^ before).is_empty()


def test_pad_free_wire_tips_stay_reported():
    layout, top, _ = _pair(cell_name="WIRE")
    before = _region(layout, top, 19)
    report = _repair(layout, top)
    assert report["m1_landing_pairs_extended"] == 0
    assert report["tip_check"]["rules"][0]["via_pads"] == 0
    assert report["tip_check"]["rules"][0]["rule"] == "M1.S.4"
    assert (_region(layout, top, 19) ^ before).is_empty()


def test_growth_respects_spacing_to_neighboring_wire():
    layout, top, _ = _pair()
    top.shapes(layout.layer(19, 0)).insert(kdb.Box(-60, 40, 90, 60))
    before = _region(layout, top, 19)
    report = _repair(layout, top)
    added = _region(layout, top, 19) - before
    assert not added.is_empty()
    # Growing up would trade M1.S.4 for tip-to-side M1.S.2 at the long wire.
    assert added.bbox().top == -11
    assert report["tip_check"]["violations"] == 0


def test_pad_covered_by_longer_wire_edge_keeps_diagnostic():
    layout, top, _ = _pair()
    # The 23 nm limiting edge overlaps the entire 22 nm landing side.
    top.shapes(layout.layer(19, 0)).insert(kdb.Box(-9, 10, 9, 12))
    before = _region(layout, top, 19)
    report = _repair(layout, top)
    assert report["tip_check"]["rules"][0]["rule"] == "M1.S.4"
    assert report["tip_check"]["rules"][0]["via_pads"] == 1
    assert (_region(layout, top, 19) ^ before).is_empty()
