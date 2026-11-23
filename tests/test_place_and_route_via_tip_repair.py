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


def _mixed_pair(gap_nm, wire_nm, *, library=False, rotation=0, cut_inset=0):
    layout = kdb.Layout()
    layout.dbu = 0.00025
    top, via = layout.create_cell("top"), layout.create_cell("VIA_VIA12")
    m1, v1 = layout.layer(19, 0), layout.layer(21, 0)
    via.shapes(m1).insert(kdb.Box(-36, -44, 36, 44))
    via.shapes(v1).insert(kdb.Box(-36 + cut_inset, -36, 36 - cut_inset, 36))
    transform = kdb.Trans(rotation, False, 0, 0)
    top.insert(kdb.CellInstArray(via.cell_index(), transform))
    x = 36 + 4 * gap_nm
    wire = kdb.Box(x, -2 * wire_nm, x + 400, 2 * wire_nm)
    if library:
        cell = layout.create_cell("STANDARD_CELL")
        cell.shapes(m1).insert(wire)
        top.insert(kdb.CellInstArray(cell.cell_index(), transform))
    else:
        top.shapes(m1).insert(wire.transformed(transform))
    return layout, top, via, wire.transformed(transform)


@pytest.mark.parametrize(
    ("gap", "width", "library", "rotation", "rule", "wire_grows"),
    [
        (22, 18, False, 0, "M1.S.4", True),
        (27, 18, False, 1, "M1.S.4", False),
        (22, 30, True, 0, "M1.S.5", True),
        (27, 30, True, 1, "M1.S.5", False),
    ],
)
def test_pad_wire_repair(gap, width, library, rotation, rule, wire_grows):
    layout, top, via, wire = _mixed_pair(gap, width, library=library, rotation=rotation)
    before, cuts = _region(layout, top, 19), _region(layout, top, 21)
    master = _region(layout, via, 19)
    check = check_via_pad_tips(
        kdb, layout=layout, top_cell=top, variant="asap7A", lef_to_gds={"M1": (19, 0)}
    )
    assert any(row["rule"] == rule for row in check["rules"])
    report = _repair(layout, top)
    after = _region(layout, top, 19)
    assert report["tip_check"]["violations"] == 0
    assert report["m1_landing_pad_wire_pairs_extended"] == 1
    assert report["m1_landing_pairs_extended"] == 0
    assert after.count() == before.count() == 2
    assert (before - after).is_empty()
    assert (after - before).interacting(cuts).is_empty()
    assert (cuts ^ _region(layout, top, 21)).is_empty()
    assert (master ^ _region(layout, via, 19)).is_empty()
    wire_growth = after.interacting(kdb.Region(wire)) - kdb.Region(wire)
    assert (not wire_growth.is_empty()) is wire_grows
    if library:
        local_wire = wire.transformed(kdb.Trans(rotation, False, 0, 0).inverted())
        original = _region(layout, layout.cell("STANDARD_CELL"), 19)
        assert (original ^ kdb.Region(local_wire)).is_empty()
    assert _repair(layout, top)["m1_landing_pad_wire_pairs_extended"] == 0


@pytest.mark.parametrize("obstruction", ["cuts", "conductors", "inset_cut"])
def test_unrepairable_pad_wire_keeps_geometry(obstruction):
    layout, top, _, _ = _mixed_pair(
        22, 18, cut_inset=4 if obstruction == "inset_cut" else 0
    )
    if obstruction != "inset_cut":
        layer = layout.layer(18 if obstruction == "cuts" else 19, 0)
        for y in (-48, 48):
            top.shapes(layer).insert(kdb.Box(-8, y - 2, 8, y + 2))
    before = _region(layout, top, 19)
    report = _repair(layout, top)
    assert report["tip_check"]["violations"] > 0
    assert report["m1_landing_pad_wire_pairs_extended"] == 0
    assert (before ^ _region(layout, top, 19)).is_empty()


@pytest.mark.parametrize("gap", [22, 27])
@pytest.mark.parametrize("placement", ["nested", "v0", "multi_cut"])
def test_facing_via_pad_is_never_repaired_as_a_wire(gap, placement):
    layout, top, via, _ = _mixed_pair(gap, 18)
    m1 = layout.layer(19, 0)
    top.shapes(m1).clear()
    offset = kdb.Trans(72 + 4 * gap, 0)
    if placement == "nested":
        other = layout.create_cell("BLOCK")
        other.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans()))
    else:
        other = layout.create_cell("VIA_OTHER")
        other.shapes(m1).insert(kdb.Box(-36, -44, 36, 44))
        if placement == "v0":
            other.shapes(layout.layer(18, 0)).insert(kdb.Box(-36, -36, 36, 36))
        else:
            cuts = other.shapes(layout.layer(21, 0))
            cuts.insert(kdb.Box(-28, -36, -4, 36))
            cuts.insert(kdb.Box(4, -36, 28, 36))
    top.insert(kdb.CellInstArray(other.cell_index(), offset))
    before = _region(layout, top, 19)
    report = _repair(layout, top)
    assert report["m1_landing_pad_wire_pairs_extended"] == 0
    assert report["m1_landing_pairs_extended"] == 0
    assert (_region(layout, top, 19) ^ before).is_empty()
    assert report["tip_check"]["violations"] > 0


def test_pad_and_wire_can_extend_in_opposite_directions():
    layout, top, _, wire = _mixed_pair(22, 18)
    v0 = layout.layer(18, 0)
    for box in (kdb.Box(-36, 40, 36, 112), kdb.Box(124, -104, 196, -32)):
        top.shapes(v0).insert(box)
    before = _region(layout, top, 19)
    cuts = _region(layout, top, 18) + _region(layout, top, 21)
    report = _repair(layout, top)
    after = _region(layout, top, 19)
    assert report["m1_landing_pad_wire_pairs_extended"] == 1
    assert report["tip_check"]["violations"] == 0
    assert after.count() == before.count() == 2
    assert (after - before).interacting(cuts).is_empty()
    assert after.interacting(kdb.Region(wire)).bbox().top > wire.top
    assert (after - after.interacting(kdb.Region(wire))).bbox().bottom < -44


def _side_pair(gap_nm, *, library=False, rotation=0):
    """A cut-flush V1 landing's short face against a long library-style edge.

    The pad is the ``_pair`` fixture's 18 x 22 nm ``VIA_VIA12`` landing; the
    neighbour's facing edge is a 100 nm side, so the pair is ``M1.S.2``
    (short vs long) rather than the ``S.4``/``S.5`` tip pairs.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top, via = layout.create_cell("top"), layout.create_cell("VIA_VIA12")
    m1, v1 = layout.layer(19, 0), layout.layer(21, 0)
    via.shapes(m1).insert(kdb.Box(-9, -11, 9, 11))
    via.shapes(v1).insert(kdb.Box(-9, -9, 9, 9))
    transform = kdb.Trans(rotation, False, 0, 0)
    top.insert(kdb.CellInstArray(via.cell_index(), transform))
    x = 9 + gap_nm
    side = kdb.Box(x, -50, x + 80, 50)
    if library:
        cell = layout.create_cell("STANDARD_CELL")
        cell.shapes(m1).insert(side)
        top.insert(kdb.CellInstArray(cell.cell_index(), transform))
    else:
        top.shapes(m1).insert(side.transformed(transform))
    return layout, top, via, side.transformed(transform)


@pytest.mark.parametrize(
    ("gap", "library", "rotation"),
    [(18, False, 0), (24, False, 0), (18, True, 0), (18, False, 1)],
)
def test_pad_side_repair(gap, library, rotation):
    layout, top, via, side = _side_pair(gap, library=library, rotation=rotation)
    before, cuts = _region(layout, top, 19), _region(layout, top, 21)
    master = _region(layout, via, 19)
    check = check_via_pad_tips(
        kdb, layout=layout, top_cell=top, variant="asap7A", lef_to_gds={"M1": (19, 0)}
    )
    assert [(row["rule"], row["violations"]) for row in check["rules"]] == [
        ("M1.S.2", 1)
    ]
    report = _repair(layout, top)
    after = _region(layout, top, 19)
    assert report["tip_check"]["violations"] == 0
    assert report["m1_landing_pad_wire_pairs_extended"] == 1
    assert report["m1_landing_pairs_extended"] == 0
    assert after.count() == before.count() == 2
    assert (before - after).is_empty()
    assert (after - before).interacting(cuts).is_empty()
    assert (cuts ^ _region(layout, top, 21)).is_empty()
    assert (master ^ _region(layout, via, 19)).is_empty()
    if library:
        local_side = side.transformed(kdb.Trans(rotation, False, 0, 0).inverted())
        original = _region(layout, layout.cell("STANDARD_CELL"), 19)
        assert (original ^ kdb.Region(local_side)).is_empty()
    assert _repair(layout, top)["m1_landing_pad_wire_pairs_extended"] == 0


@pytest.mark.parametrize("side", [-1, 1])
def test_side_growth_avoids_contact_on_one_side(side):
    layout, top, _, _ = _side_pair(18)
    # A V0 just outside the original M1 cannot be connected by the edit.
    y = side * 14
    top.shapes(layout.layer(18, 0)).insert(kdb.Box(-4, y - 1, 4, y + 1))
    before = _region(layout, top, 19)
    assert _repair(layout, top)["m1_landing_pad_wire_pairs_extended"] == 1
    added = _region(layout, top, 19) - before
    assert added.interacting(_region(layout, top, 18)).is_empty()
    assert (added.bbox().top if side > 0 else -added.bbox().bottom) == -11


def test_unrepairable_side_keeps_diagnostic():
    layout, top, _, _ = _side_pair(18)
    for y in (-14, 14):
        top.shapes(layout.layer(18, 0)).insert(kdb.Box(-4, y - 1, 4, y + 1))
    before = _region(layout, top, 19)
    report = _repair(layout, top)
    assert report["m1_landing_pad_wire_pairs_extended"] == 0
    assert report["tip_check"]["rules"][0]["rule"] == "M1.S.2"
    assert (_region(layout, top, 19) ^ before).is_empty()


def test_side_pair_on_a_pad_also_in_an_s4_pair_repairs_once():
    layout = kdb.Layout()
    layout.dbu = 0.001
    top, via = layout.create_cell("top"), layout.create_cell("VIA_VIA12")
    m1, v1 = layout.layer(19, 0), layout.layer(21, 0)
    via.shapes(m1).insert(kdb.Box(-9, -11, 9, 11))
    via.shapes(v1).insert(kdb.Box(-9, -9, 9, 9))
    for x in (0, 36):
        top.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(x, 0)))
    # The second pad's right edge is both an S.4 face to the first pad and an
    # S.2 face to the long side; the S.4 repair must resolve both at once.
    top.shapes(m1).insert(kdb.Box(63, -50, 143, 50))
    check = check_via_pad_tips(
        kdb, layout=layout, top_cell=top, variant="asap7A", lef_to_gds={"M1": (19, 0)}
    )
    assert {(row["rule"], row["violations"]) for row in check["rules"]} == {
        ("M1.S.4", 1),
        ("M1.S.2", 1),
    }
    report = _repair(layout, top)
    assert report["m1_landing_pairs_extended"] == 1
    assert report["m1_landing_pad_wire_pairs_extended"] == 0
    assert report["tip_check"]["violations"] == 0
    pad2 = _region(layout, top, 19).interacting(kdb.Region(kdb.Box(27, -11, 45, 11)))
    assert pad2.bbox().height() == 37
