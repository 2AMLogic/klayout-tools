"""Parallel-track alignment controls for ASAP7 Tables 3.11.1 and 3.13.1."""

from itertools import product
from pathlib import Path

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

# Cut, upper wire, common lower landing (GDS drawing layers).
_LAYERS = {
    "V0": (18, 19, 17),
    "V1": (21, 20, 19),
    "V2": (25, 30, 20),
    "V3": (35, 40, 30),
}
# The first cut always spans [0, 18] nm along its wire.
_SECOND = {
    "aligned": (0, 18),
    "shared_left": (0, 24),
    "shifted": (5, 23),
    "shared_right": (-6, 18),
    "nested": (-2, 22),
}
# V0–V2 use the shared-left case in both orientations. V3 also checks its mirror.
_CASES = [
    *product(
        ("V0", "V1", "V2"),
        (0, 1),
        (18, 22, 27),
        ("aligned", "shared_left", "shifted", "nested"),
    ),
    *product(("V3",), (0,), (24,), _SECOND),
]


@pytest.fixture(scope="module")
def track_pairs(tmp_path_factory):
    layout = kdb.Layout()
    layout.dbu = 0.00025
    top = layout.create_cell("TOP")
    for i, (family, rotation, gap, alignment) in enumerate(_CASES):
        cell = layout.create_cell(f"{family}_{rotation}_{gap}_{alignment}")
        cut, upper, lower = _LAYERS[family]
        width = 24 if family == "V3" else 18
        # M4 extends at least 11 nm beyond both ends of every V3.
        left, right = (-20, 48) if family == "V3" else (-8, 38)
        rotate = kdb.Trans(rotation, False, 0, 0)
        for (x1, x2), y in zip(
            ((0, 18), _SECOND[alignment]), (0, width + gap), strict=True
        ):
            via = kdb.Box(x1, y, x2, y + width)
            wire = kdb.Box(left, y, right, y + width)
            cell.shapes(layout.layer(cut, 0)).insert((via * 4).transformed(rotate))
            cell.shapes(layout.layer(upper, 0)).insert((wire * 4).transformed(rotate))
        landing = kdb.Box(left, -8, right, 2 * width + gap + 8)
        cell.shapes(layout.layer(lower, 0)).insert((landing * 4).transformed(rotate))
        # Keep pairs isolated while preserving the horizontal M4 grid.
        top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(i * 4000, 0)))
    path = tmp_path_factory.mktemp("track-spacing") / "pairs.gds"
    layout.write(str(path))
    saved = kdb.Layout()
    saved.read(str(path))
    return saved, path


@pytest.mark.parametrize(("family", "rotation", "gap", "alignment"), _CASES)
def test_parallel_track_readings(track_pairs, family, rotation, gap, alignment):
    layout, _ = track_pairs
    cell = layout.cell(f"{family}_{rotation}_{gap}_{alignment}")
    cut, upper, _ = _LAYERS[family]
    undo = kdb.Trans(rotation, False, 0, 0).inverted()

    def along_wire(layer):
        return sorted(
            (
                s.box.transformed(undo)
                for s in cell.shapes(layout.layer(layer, 0)).each()
            ),
            key=lambda box: box.bottom,
        )

    cuts = along_wire(cut)
    for via, wire in zip(cuts, along_wire(upper), strict=True):
        assert wire.left < via.left < via.right < wire.right
        assert (wire.bottom, wire.top) == (via.bottom, via.top)
    a, b = cuts
    clearance = b.bottom - a.top
    overlap = min(a.right, b.right) - max(a.left, b.left)
    shared_end = a.left == b.left or a.right == b.right
    shorter_covered = overlap == min(a.width(), b.width())
    assert clearance == gap * 4
    assert overlap == (13 if alignment == "shifted" else 18) * 4
    # Both readings reproduce the full, partial and unaligned examples
    # in Fig. 1.2.6(a-c). Only strict containment has no pictured verdict.
    passes_shared_end = clearance >= (18 if shared_end else 27) * 4
    passes_containment = clearance >= (18 if shorter_covered else 27) * 4
    if alignment == "nested":
        assert (passes_shared_end, passes_containment) == (gap >= 27, True)
    else:
        expected = gap >= (27 if alignment == "shifted" else 18)
        assert (passes_shared_end, passes_containment) == (expected, expected)


@_needs_klayout
def test_parallel_track_controls_pass_the_enabled_deck(track_pairs):
    _, path = track_pairs
    report = run_drc_klayout_engine(str(path), str(DECK_PATH))
    assert set(report["coverage"]["rule_categories"]) == set(RULE_IDS)
    assert report["violations"] == []


@pytest.mark.parametrize("family", _LAYERS)
def test_partial_alignment_remains_a_measured_gap(family):
    rule = f"{family}.S.1"
    gap = next(g for g in NOT_IMPLEMENTED if g.id == rule)
    assert rule not in RULE_IDS
    assert gap.table == ("Table 3.11.1" if family == "V0" else "Table 3.13.1")
    assert "track assignment" in gap.reason
    assert "Fig. 1.2.6" in gap.reason and "strictly nested" in gap.reason
    row = next(
        line
        for line in _docs_asap7_section().splitlines()
        if line.startswith(f"| `{rule}` |")
    )
    assert gap.reason in row


def _count_pairs(layout, cell, cut_layer):
    """Count distinct cut pairs within 27 nm, and those separating the two readings.

    Try both routing axes before track assignment. A disagreement requires
    strictly nested along-track projections and [18, 27) nm clearance.
    """
    nm = round(0.001 / layout.dbu)
    assert nm * layout.dbu == pytest.approx(0.001)
    limit = 27 * nm
    cuts = kdb.Region(cell.begin_shapes_rec(layout.layer(cut_layer, 0)))
    assert all(p.is_box() for p in cuts.each())
    boxes = {p.bbox() for p in cuts.each()}
    index = kdb.Shapes()
    for box in boxes:
        index.insert(box)

    def strictly_nested(lo1, hi1, lo2, hi2):
        return lo1 < lo2 < hi2 < hi1 or lo2 < lo1 < hi1 < hi2

    nearby = distinguishing = 0
    for a in boxes:
        for shape in index.each_touching(a.enlarged(limit)):
            b = shape.box
            if not a < b:
                continue
            dx = max(a.left, b.left) - min(a.right, b.right)
            dy = max(a.bottom, b.bottom) - min(a.top, b.top)
            if max(dx, 0) ** 2 + max(dy, 0) ** 2 > limit**2:
                continue
            nearby += 1
            if (
                18 * nm <= dy < limit
                and strictly_nested(a.left, a.right, b.left, b.right)
            ) or (
                18 * nm <= dx < limit
                and strictly_nested(a.bottom, a.top, b.bottom, b.top)
            ):
                distinguishing += 1
    return nearby, distinguishing


def test_census_counts_nested_controls_on_both_axes(track_pairs):
    layout, _ = track_pairs
    for family, (cut, _, _) in _LAYERS.items():
        expected = (5, 1) if family == "V3" else (24, 4)
        assert _count_pairs(layout, layout.top_cell(), cut) == expected


def test_routed_slice_has_no_distinguishing_pair():
    layout = kdb.Layout()
    layout.read(
        str(
            Path(__file__).resolve().parents[1]
            / "blocks/asap7-ucie-phy/output/slice_routed.gds"
        )
    )
    for (cut, _, _), count in zip(
        _LAYERS.values(), (15481, 93410, 1508, 0), strict=True
    ):
        assert _count_pairs(layout, layout.top_cell(), cut) == (count, 0)


@pytest.mark.parametrize(("lib", "suffix"), _LIBRARIES)
def test_shipped_masters_have_no_distinguishing_pair(lib, suffix):
    layout = kdb.Layout()
    layout.read(str(_shipped_gds(lib, suffix)))
    cells = layout.top_cells()
    assert len(cells) == 212
    for (cut, _, _), count in zip(_LAYERS.values(), (1737, 7, 0, 0), strict=True):
        per_master = [_count_pairs(layout, cell, cut) for cell in cells]
        assert sum(nearby for nearby, _ in per_master) == count
        assert all(distinguishing == 0 for _, distinguishing in per_master)
