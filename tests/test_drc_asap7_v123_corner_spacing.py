"""Geometry probes for the retained V1-V3 corner-spacing gaps (ASAP7 Fig. 3.13.1).

Table 3.13.1 (by section 3.13) selects each via's corner-spacing bound by the
upper-metal end facing the other cut: two complete 5 nm caps need 23 nm, one
needs 27 nm, two flush ends need 30 nm. These probes pin why the three rules
per via family stay gaps: the facing end is chosen by the local landing
direction, which the table and figure fix for neither M2/M3 junctions nor --
outside the rules that already cover it -- M4.
"""

from collections import Counter
from pathlib import Path

import klayout.db as kdb
import pytest

from helpers.asap7_shipped_cells import place_between_neighbours
from klayout_tools.decks.asap7_rules import DECK_PATH, NOT_IMPLEMENTED, RULE_IDS
from klayout_tools.drc import run_drc_klayout_engine
from test_drc_asap7_deck import (
    _LIBRARIES,
    _docs_asap7_section,
    _needs_klayout,
    _shipped_gds,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SLICE_GDS = _REPO_ROOT / "blocks" / "asap7-ucie-phy" / "output" / "slice_routed.gds"

#: family -> (via layer, upper metal, lower metal, cut length, cut width) in
#: nm; the cut length runs along the landing wire (V3 is 18 x 24 because M4's
#: minimum width, which V3.M4.AUX.2 makes the cut span, is 24 nm).
_FAMILIES = {
    "V1": ((21, 0), (20, 0), (19, 0), 18, 18),
    "V2": ((25, 0), (30, 0), (20, 0), 18, 18),
    "V3": ((35, 0), (40, 0), (30, 0), 18, 24),
}

#: family -> (distinct cut sites, diagonal pairs below 30 nm, pairs flagged
#: with both facing ends read horizontally, vertically) measured on the
#: pinned routed slice (blocks/asap7-ucie-phy/output/slice_routed.gds).
_SLICE_CENSUS = {
    "V1": (108803, 2357, 0, 2357),
    "V2": (17372, 1104, 1104, 0),
    "V3": (4347, 0, 0, 0),
}


def _diagonal_pairs(boxes, max_distance):
    """Each unordered pair of distinct cuts once: projections disjoint on
    both axes, closest corners strictly below ``max_distance`` (DBU).
    Spatially binned, so routed corpora with 1e5 cuts stay fast."""
    span = max(max(b.width(), b.height()) for b in boxes)
    bin_size = max_distance + span
    grid: dict[tuple[int, int], list[int]] = {}
    for index, box in enumerate(boxes):
        for gx in range(box.left // bin_size, box.right // bin_size + 1):
            for gy in range(box.bottom // bin_size, box.top // bin_size + 1):
                grid.setdefault((gx, gy), []).append(index)
    for index, first in enumerate(boxes):
        candidates = set()
        for gx in range(
            (first.left - max_distance) // bin_size,
            (first.right + max_distance) // bin_size + 1,
        ):
            for gy in range(
                (first.bottom - max_distance) // bin_size,
                (first.top + max_distance) // bin_size + 1,
            ):
                candidates.update(grid.get((gx, gy), ()))
        for other in sorted(candidates):
            if other <= index:
                continue
            second = boxes[other]
            dx = second.left - first.right
            dx = dx if dx > 0 else first.left - second.right
            if dx <= 0 or dx > max_distance:
                continue
            dy = second.bottom - first.top
            dy = dy if dy > 0 else first.bottom - second.top
            if dy <= 0:
                continue
            d2 = dx * dx + dy * dy
            if d2 < max_distance * max_distance:
                yield first, second, d2


class _NearbyMetal:
    """Grid-indexed merged metal answering "the metal around this cut"
    without a full-layer boolean per strip (1e5-cut corpora need that)."""

    def __init__(self, merged, margin, cell=500):
        self._margin = margin
        self._cell = cell
        self._bins: dict[tuple[int, int], list[kdb.Polygon]] = {}
        for polygon in merged.each():
            box = polygon.bbox()
            for gx in range(box.left // cell, box.right // cell + 1):
                for gy in range(box.bottom // cell, box.top // cell + 1):
                    self._bins.setdefault((gx, gy), []).append(polygon)

    def around(self, cut):
        margin = self._margin
        window = kdb.Box(
            cut.left - margin, cut.bottom - margin, cut.right + margin, cut.top + margin
        )
        found: dict[tuple[int, int, int, int], kdb.Polygon] = {}
        for gx in range(window.left // self._cell, window.right // self._cell + 1):
            for gy in range(window.bottom // self._cell, window.top // self._cell + 1):
                for polygon in self._bins.get((gx, gy), ()):
                    box = polygon.bbox()
                    found[(box.left, box.bottom, box.right, box.top)] = polygon
        return kdb.Region(list(found.values())) & kdb.Region(window)


def _caps(cut, metal, depth):
    """Full-width upper-metal coverage to the left, right, bottom and top."""
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


def _pair_layout(tmp_path, family, dx, dy, caps, rotation, junction=False):
    """Two cuts of one family on separate straight upper-metal wires at a
    (dx, dy) DBU diagonal. The far end always carries a 5 nm cap, the
    facing end ``caps[i]``; a junction adds an arm across the track that
    reaches 5 nm past both cut edges. One shared lower landing serves both
    cuts (the table conditions only the upper metal)."""
    via_layer, upper_layer, lower_layer, along, across = _FAMILIES[family]
    nm = 20  # 0.05 nm/DBU admits exact 3:4:5 diagonals at all three bounds.
    layout = kdb.Layout()
    layout.dbu = 0.001 / nm
    cell = layout.create_cell("TOP")
    trans = kdb.Trans(rotation, False, 0, 0)
    for i, (x, y) in enumerate(((0, 0), (along * nm + dx, across * nm + dy))):
        via = kdb.Box(x, y, x + along * nm, y + across * nm)
        west, east = (5, caps[i]) if i == 0 else (caps[i], 5)
        upper = kdb.Box(x - west * nm, y, x + (along + east) * nm, y + across * nm)
        cell.shapes(layout.layer(*via_layer)).insert(via.transformed(trans))
        layer = cell.shapes(layout.layer(*upper_layer))
        layer.insert(upper.transformed(trans))
        if junction:
            # An arm across the track gives the other axis a whole 5 nm strip.
            arm = kdb.Box(x, y - 5 * nm, x + along * nm, y + (across + 5) * nm)
            layer.insert(arm.transformed(trans))
    lower = kdb.Box(
        -6 * nm,
        -6 * nm,
        2 * along * nm + dx + 6 * nm,
        2 * across * nm + dy + 6 * nm,
    )
    cell.shapes(layout.layer(*lower_layer)).insert(lower.transformed(trans))
    path = tmp_path / "pair.gds"
    layout.write(str(path))
    saved = kdb.Layout()
    saved.read(str(path))
    top_cell = saved.top_cells()[0]
    boxes = [s.box for s in top_cell.shapes(saved.layer(*via_layer)).each()]
    return boxes, kdb.Region(top_cell.shapes(saved.layer(*upper_layer))).merged(), nm


def _v3_layout(tmp_path, dx, dy, facing, far, arm=False):
    """Two 18 x 24 nm V3 cuts on their own exact-width M4 wires, the facing
    end of cut 0 (east) and cut 1 (west) carrying its element of ``facing``
    and both far ends ``far`` (nm; 0.25 nm/DBU admits the 10.75 nm case)."""
    via_layer, upper_layer, lower_layer, along, across = _FAMILIES["V3"]
    q = 4
    layout = kdb.Layout()
    layout.dbu = 0.001 / q
    cell = layout.create_cell("TOP")
    for i, (x, y) in enumerate(((0, 0), (along + dx, across + dy))):
        west, east = (far, facing[i]) if i == 0 else (facing[i], far)
        via = kdb.Box(x * q, y * q, (x + along) * q, (y + across) * q)
        cell.shapes(layout.layer(*via_layer)).insert(via)
        layer = cell.shapes(layout.layer(*upper_layer))
        layer.insert(
            kdb.Box((x - west) * q, y * q, (x + along + east) * q, (y + across) * q)
        )
        if arm:
            layer.insert(
                kdb.Box(x * q, (y - 5) * q, (x + along) * q, (y + across + 5) * q)
            )
    lower = kdb.Box(-6 * q, -6 * q, (2 * along + dx + 6) * q, (2 * across + dy + 6) * q)
    cell.shapes(layout.layer(*lower_layer)).insert(lower)
    path = tmp_path / "v3.gds"
    layout.write(str(path))
    return path


@pytest.mark.parametrize(
    "rule", [f"V{via}.S.{n}" for via in (1, 2, 3) for n in (2, 3, 4)]
)
def test_registry_keeps_the_measured_corner_spacing_gaps(rule):
    gap = next(g for g in NOT_IMPLEMENTED if g.id == rule)
    assert gap.table == "Table 3.13.1" and gap.id not in RULE_IDS
    if rule.startswith("V3"):
        assert "59.09 nm" in gap.reason and "V3.M4.EN.2" in gap.reason
    else:
        assert "2,357" in gap.reason and "1,104" in gap.reason
    assert gap.reason in _docs_asap7_section()


@pytest.mark.parametrize("family", ["V1", "V2", "V3"])
@pytest.mark.parametrize("rotation", [0, 1])
@pytest.mark.parametrize(
    ("caps", "limit"), [((5, 5), 23), ((0, 5), 27), ((5, 0), 27), ((0, 0), 30)]
)
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_straight_wires_use_the_facing_ends_at_the_boundary(
    tmp_path, family, rotation, caps, limit, delta
):
    dx, dy = 12 * limit + delta, 16 * limit
    boxes, metal, nm = _pair_layout(tmp_path, family, dx, dy, caps, rotation)
    [(a, b, d2)] = _diagonal_pairs(boxes, 31 * nm)
    endpoints = [
        _facing(cut, other, _caps(cut, metal, 5 * nm))[rotation]
        for cut, other in ((a, b), (b, a))
    ]
    assert d2 == dx**2 + dy**2
    assert sum(endpoints) == sum(c == 5 for c in caps)
    assert _violates(d2, *endpoints, nm) == (delta < 0)


@pytest.mark.parametrize("family", ["V1", "V2", "V3"])
@pytest.mark.parametrize("rotation", [0, 1])
def test_junction_landings_leave_both_axes_supported(tmp_path, family, rotation):
    boxes, metal, nm = _pair_layout(
        tmp_path, family, 20 * 20, 15 * 20, (0, 0), rotation, junction=True
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
@pytest.mark.parametrize("family", ["V1", "V2"])
@pytest.mark.parametrize("rotation", [0, 1])
def test_junction_control_passes_the_enabled_deck(tmp_path, family, rotation):
    _pair_layout(tmp_path, family, 20 * 20, 15 * 20, (0, 0), rotation, junction=True)
    report = run_drc_klayout_engine(str(tmp_path / "pair.gds"), str(DECK_PATH))
    assert set(report["coverage"]["rule_categories"]) == set(RULE_IDS)
    assert report["violations"] == []


@_needs_klayout
def test_v3_junction_arm_breaks_the_exact_width_rule(tmp_path):
    """No deck-clean V3 junction exists: an arm across the track wide enough
    for a whole 5 nm strip puts the cut's horizontal edges inside M4, which
    V3.M4.AUX.2 reports, so the ambiguous facing-end choice the V0/V1/V2
    junctions expose cannot arise on ruled V3 geometry."""
    _v3_layout(tmp_path, 7, 24, (0, 0), 30, arm=True)
    report = run_drc_klayout_engine(str(tmp_path / "v3.gds"), str(DECK_PATH))
    counts = Counter(v["rule"] for v in report["violations"])
    assert counts["V3.M4.AUX.2"] == 4  # both horizontal edges of each cut
    assert counts["V3.M4.EN.2"] == 2  # the arm's 5 nm is below the 11 nm bound


@_needs_klayout
@pytest.mark.parametrize(
    ("facing", "markers"),
    [
        ((0, 0), 2),
        ((0, 5), 2),
        ((5, 5), 2),
        ((10.75, 11), 1),
        ((11, 11), 0),
        ((12, 15), 0),
    ],
)
def test_v3_facing_extensions_differ_from_the_5_nm_strip_selector(
    tmp_path, facing, markers
):
    """At a 25 nm corner separation (a 7-24-25 diagonal) the existing 11 nm
    enclosure check, not the corner-spacing bounds, answers every facing
    extension below 11 nm -- including the whole 0-5 nm range the 5 nm strip
    selector would need to separate the 23/27/30 nm bounds."""
    _v3_layout(tmp_path, 7, 24, facing, 30)
    report = run_drc_klayout_engine(str(tmp_path / "v3.gds"), str(DECK_PATH))
    counts = Counter(v["rule"] for v in report["violations"])
    assert counts["V3.M4.EN.2"] == markers
    assert counts["V3.M4.AUX.2"] == 0


@pytest.mark.parametrize("family", ["V1", "V2", "V3"])
def test_routed_slice_close_pairs_have_one_supported_axis(family):
    layout = kdb.Layout()
    layout.read(str(_SLICE_GDS))
    (top,) = layout.top_cells()
    nm = round(0.001 / layout.dbu)
    via_layer, metal_layer, _, _, _ = _FAMILIES[family]
    vias = kdb.Region(top.begin_shapes_rec(layout.layer(*via_layer)))
    assert all(p.is_box() for p in vias.each())
    boxes = [p.bbox() for p in vias.each()]
    metal = _NearbyMetal(
        kdb.Region(top.begin_shapes_rec(layout.layer(*metal_layer))).merged(),
        16 * nm,
    )
    pairs = horizontal = vertical = strip = 0
    for a, b, d2 in _diagonal_pairs(boxes, 30 * nm):
        pairs += 1
        ends = []
        for cut, other in ((a, b), (b, a)):
            coverage = _caps(cut, metal.around(cut), 5 * nm)
            axes = (coverage[0] or coverage[1], coverage[2] or coverage[3])
            assert sum(axes) == 1  # exactly one supported axis per via
            ends.append((_facing(cut, other, coverage), axes))
        (fa, axa), (fb, axb) = ends
        horizontal += _violates(d2, fa[0], fb[0], nm)
        vertical += _violates(d2, fa[1], fb[1], nm)
        chosen = (fa[0] if axa[0] else fa[1], fb[0] if axb[0] else fb[1])
        strip += _violates(d2, *chosen, nm)
    assert (len(boxes), pairs, horizontal, vertical) == _SLICE_CENSUS[family]
    assert strip == 0


def test_routed_slice_has_no_close_v3_pair_and_caps_every_end():
    layout = kdb.Layout()
    layout.read(str(_SLICE_GDS))
    (top,) = layout.top_cells()
    nm = round(0.001 / layout.dbu)
    via_layer, metal_layer, _, _, _ = _FAMILIES["V3"]
    vias = kdb.Region(top.begin_shapes_rec(layout.layer(*via_layer)))
    boxes = [p.bbox() for p in vias.each()]
    best = min((d2 for _, _, d2 in _diagonal_pairs(boxes, 200 * nm)), default=None)
    assert round((best**0.5) / nm, 2) == 59.09
    metal = _NearbyMetal(
        kdb.Region(top.begin_shapes_rec(layout.layer(*metal_layer))).merged(),
        16 * nm,
    )
    for cut in set(boxes):
        coverage = _caps(cut, metal.around(cut), 11 * nm)
        assert coverage[0] and coverage[1]  # an 11 nm strip on both ends


@pytest.mark.parametrize(("lib", "suffix"), _LIBRARIES)
def test_placed_shipped_cells_reproduce_the_library_census(tmp_path, lib, suffix):
    """The sandwiched 212-master corpus (the clean-run gate's placement)
    holds 389 V1 sites per library; its six diagonal pairs below 30 nm all
    pass with each cut's sole supported axis while the vertical reading
    flags every one, matching the routed-slice census."""
    gds = _shipped_gds(lib, suffix)
    out = tmp_path / "placed.gds"
    names = place_between_neighbours(gds, f"INVx1_ASAP7_75t_{suffix}", out)
    layout = kdb.Layout()
    layout.read(str(out))
    (top,) = layout.top_cells()
    nm = round(0.001 / layout.dbu)
    boxes = [
        p.bbox() for p in kdb.Region(top.begin_shapes_rec(layout.layer(21, 0))).each()
    ]
    metal = _NearbyMetal(
        kdb.Region(top.begin_shapes_rec(layout.layer(20, 0))).merged(), 16 * nm
    )
    assert len(names) == 212 and len(boxes) == 389
    pairs = horizontal = vertical = strip = 0
    for a, b, d2 in _diagonal_pairs(boxes, 30 * nm):
        pairs += 1
        ends = []
        for cut, other in ((a, b), (b, a)):
            coverage = _caps(cut, metal.around(cut), 5 * nm)
            axes = (coverage[0] or coverage[1], coverage[2] or coverage[3])
            assert sum(axes) == 1  # exactly one supported axis per via
            ends.append((_facing(cut, other, coverage), axes))
        (fa, axa), (fb, axb) = ends
        horizontal += _violates(d2, fa[0], fb[0], nm)
        vertical += _violates(d2, fa[1], fb[1], nm)
        chosen = (fa[0] if axa[0] else fa[1], fb[0] if axb[0] else fb[1])
        strip += _violates(d2, *chosen, nm)
    assert (pairs, horizontal, vertical, strip) == (6, 0, 6, 0)
