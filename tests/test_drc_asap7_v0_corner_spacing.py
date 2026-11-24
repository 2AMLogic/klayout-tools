"""Geometry probes for the retained V0.S.2–S.4 gaps (ASAP7 Fig. 3.11.1)."""

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7_rules import DECK_PATH, NOT_IMPLEMENTED, RULE_IDS
from klayout_tools.drc import run_drc_klayout_engine
from test_drc_asap7_deck import (
    _LIBRARIES,
    _docs_asap7_section,
    _needs_klayout,
    _shipped_gds,
)


def _diagonal_pairs(boxes, max_distance):
    """Closest corners on distinct rectangular cuts with disjoint projections."""
    ordered = sorted(set(boxes), key=lambda b: (b.left, b.bottom, b.right, b.top))
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            dx = right.left - left.right
            if dx > max_distance:
                break
            dy = max(right.bottom - left.top, left.bottom - right.top)
            squared_distance = dx**2 + dy**2
            if dx > 0 and dy > 0 and squared_distance <= max_distance**2:
                yield left, right, squared_distance


def _caps(cut, metal, depth):
    """Full-width M1 coverage to the left, right, bottom and top of the cut."""
    left, bottom, right, top = cut.left, cut.bottom, cut.right, cut.top
    strips = (
        kdb.Box(left - depth, bottom, left, top),
        kdb.Box(right, bottom, right + depth, top),
        kdb.Box(left, bottom - depth, right, bottom),
        kdb.Box(left, top, right, top + depth),
    )
    return tuple((kdb.Region(strip) - metal).is_empty() for strip in strips)


def _facing(cut, other, coverage):
    left, right, bottom, top = coverage
    return (
        right if cut.right < other.left else left,
        top if cut.top < other.bottom else bottom,
    )


def _violates(distance_squared, a, b, nm):
    limit = (30, 27, 23)[int(a) + int(b)]
    return distance_squared < (limit * nm) ** 2


def _pair_layout(tmp_path, dx, dy, caps, rotation, junction=False):
    # 0.05 nm/DBU admits exact 3:4:5 diagonals at all three thresholds.
    nm = 20
    layout = kdb.Layout()
    layout.dbu = 0.001 / nm
    cell = layout.create_cell("TOP")
    trans = kdb.Trans(rotation, False, 0, 0)
    side = 18 * nm
    for i, (x, y) in enumerate(((0, 0), (side + dx, side + dy))):
        via = kdb.Box(x, y, x + side, y + side)
        # Every cut has a 5 nm cap on its far end, independent of the near end.
        west, east = (5, caps[i]) if i == 0 else (caps[i], 5)
        m1 = kdb.Box(x - west * nm, y, x + side + east * nm, y + side)
        cell.shapes(layout.layer(18, 0)).insert(via.transformed(trans))
        layer = cell.shapes(layout.layer(19, 0))
        layer.insert(m1.transformed(trans))
        if junction:
            layer.insert(
                kdb.Box(x, y - 5 * nm, x + side, y + side + 5 * nm).transformed(trans)
            )
    if junction:
        # A common LISD landing supplies the lower conductor of both cuts.
        lower = kdb.Box(
            -3 * nm, -3 * nm, 2 * side + dx + 3 * nm, 2 * side + dy + 3 * nm
        )
        cell.shapes(layout.layer(17, 0)).insert(lower.transformed(trans))
    path = tmp_path / "pair.gds"
    layout.write(str(path))
    saved = kdb.Layout()
    saved.read(str(path))
    top = saved.top_cell()
    boxes = [s.box for s in top.shapes(saved.layer(18, 0)).each()]
    return boxes, kdb.Region(top.shapes(saved.layer(19, 0))).merged(), nm


def test_registry_records_facing_end_ambiguity():
    section = _docs_asap7_section()
    for rule in ("V0.S.2", "V0.S.3", "V0.S.4"):
        gap = next(g for g in NOT_IMPLEMENTED if g.id == rule)
        assert gap.table == "Table 3.11.1" and gap.id not in RULE_IDS
        assert "T-shaped" in gap.reason and "25 nm" in gap.reason
        assert gap.reason in section


@pytest.mark.parametrize("rotation", [0, 1])
@pytest.mark.parametrize(
    ("caps", "limit"), [((5, 5), 23), ((0, 5), 27), ((5, 0), 27), ((0, 0), 30)]
)
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_straight_wires_use_the_facing_ends_at_the_boundary(
    tmp_path, rotation, caps, limit, delta
):
    dx, dy = 12 * limit + delta, 16 * limit
    boxes, metal, nm = _pair_layout(tmp_path, dx, dy, caps, rotation)
    [(a, b, d2)] = _diagonal_pairs(boxes, 31 * nm)
    endpoints = [
        _facing(cut, other, _caps(cut, metal, 5 * nm))[rotation]
        for cut, other in ((a, b), (b, a))
    ]
    assert d2 == dx**2 + dy**2
    assert sum(endpoints) == sum(c == 5 for c in caps)
    assert _violates(d2, *endpoints, nm) == (delta < 0)


@pytest.mark.parametrize("rotation", [0, 1])
def test_two_junctions_change_the_applicable_bound(tmp_path, rotation):
    boxes, metal, nm = _pair_layout(
        tmp_path, 20 * 20, 15 * 20, (0, 0), rotation, junction=True
    )
    [(a, b, d2)] = _diagonal_pairs(boxes, 30 * nm)
    capped = [
        _facing(cut, other, _caps(cut, metal, 5 * nm))
        for cut, other in ((a, b), (b, a))
    ]
    assert d2 == (25 * nm) ** 2
    assert capped[0] == capped[1] == (bool(rotation), not bool(rotation))
    # Each axis has at least one whole 5 nm cap on each via.
    for cut in boxes:
        left, right, bottom, top = _caps(cut, metal, 5 * nm)
        assert (left or right) and (bottom or top)
    assert [_violates(d2, *pair, nm) for pair in zip(*capped, strict=True)] == [
        rotation == 0,
        rotation == 1,
    ]


@_needs_klayout
@pytest.mark.parametrize("rotation", [0, 1])
def test_junction_control_passes_the_enabled_deck(tmp_path, rotation):
    _pair_layout(tmp_path, 20 * 20, 15 * 20, (0, 0), rotation, junction=True)
    report = run_drc_klayout_engine(str(tmp_path / "pair.gds"), str(DECK_PATH))
    assert set(report["coverage"]["rule_categories"]) == set(RULE_IDS)
    assert report["violations"] == []


def test_isolated_cut_and_side_to_side_cuts_produce_no_corner_pair():
    a = kdb.Box(0, 0, 72, 72)
    assert list(_diagonal_pairs([a], 120)) == []
    for bottom in (0, 71):
        b = kdb.Box(80, bottom, 152, bottom + 72)
        assert list(_diagonal_pairs([a, b], 120)) == []


@pytest.mark.parametrize(("lib", "suffix"), _LIBRARIES)
def test_shipped_corner_pairs_have_one_supported_axis_per_via(lib, suffix):
    layout = kdb.Layout()
    layout.read(str(_shipped_gds(lib, suffix)))
    nm = round(0.001 / layout.dbu)
    cells = layout.top_cells()
    cut_count = pair_count = horizontal_flags = vertical_flags = 0
    for cell in cells:
        vias = kdb.Region(cell.begin_shapes_rec(layout.layer(18, 0)))
        assert all(p.is_box() for p in vias.each())
        boxes = {p.bbox() for p in vias.each()}
        cut_count += len(boxes)
        metal = kdb.Region(cell.begin_shapes_rec(layout.layer(19, 0))).merged()
        for a, b, d2 in _diagonal_pairs(boxes, 30 * nm):
            if d2 == (30 * nm) ** 2:
                continue
            pair_count += 1
            possible = []
            facing = []
            for cut, other in ((a, b), (b, a)):
                coverage = _caps(cut, metal, 5 * nm)
                left, right, bottom, top = coverage
                axes = (left or right, bottom or top)
                assert sum(axes) == 1
                ends = _facing(cut, other, coverage)
                facing.append(ends)
                possible.append(ends[axes.index(True)])
            assert not _violates(d2, *possible, nm)
            horizontal_flags += _violates(d2, facing[0][0], facing[1][0], nm)
            vertical_flags += _violates(d2, facing[0][1], facing[1][1], nm)
    assert (len(cells), cut_count, pair_count) == (212, 8576, 29)
    assert (horizontal_flags, vertical_flags) == (1, 28)
