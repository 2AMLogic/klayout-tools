"""Zero-area edge-pair markers report as 2-point segments (issue #5869).

An ``enclosing`` violation whose deficit is *total* -- the enclosed edge
exactly coincident with the enclosing edge (zero extension) -- makes
``EdgePair.polygon(0).each_point_hull()`` return a self-touching,
zero-area quad: duplicated vertex, an interior collinear point, area 0.
Such a marker localises nothing along the edge and breaks downstream
simple-polygon assumptions. A collinear hull now collapses to the two
extreme points of the failing edge; non-degenerate hulls are
byte-identical to before, and the genuinely unrenderable cases (the empty
hull of a single-point marker -- issue #2642) stay ``null``.

The pinned polygon literals below were captured by running the same
layouts through the pre-#5869 engine (KLayout 0.30.12), so the
untouched-case assertions are exact rather than structural.

Perimeter order (issue #7979): an ``enclosed``/``enclosing`` pair runs both
edges the same direction, so KLayout's own ``polygon(0)`` walk of it is a
self-crossing bowtie. Those rings are now re-walked around the perimeter
(same corners for parallel edges); rings that were already simple -- every
``space``/``width``/``separation`` marker -- keep KLayout's vertex sequence.
The literals for the crossed pairs below were re-captured accordingly, and
``assert_simple_quad`` checks them structurally as well, because a signed
area cannot tell a bowtie from a perimeter when its lobes are equal.
"""

from __future__ import annotations

import random

import klayout.db as kdb
import pytest

import klayout_tools.drc as drc
from helpers.drc_marker_ring import assert_simple_quad, first_meeting_sides
from klayout_tools._drc_edge_pair_hull import _edge_pair_hull_points
from klayout_tools.decks import DrcRule

#: The repro geometry: layer A encloses layer B, flush (zero extension)
#: on the left and right edges, ample margin top and bottom. Layout dbu
#: is 0.001, so coordinates below are dbu as drawn.
REPRO_A = (0, 0, 1000, 2000)
REPRO_B = (0, 100, 1000, 1900)


def _write_two_layer_layout(tmp_path, a_box, b_box) -> str:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(*a_box))
    top.shapes(layout.layer(2, 0)).insert(kdb.Box(*b_box))
    path = tmp_path / "enclosing.gds"
    layout.write(str(path))
    return str(path)


def _run_with_deck(monkeypatch, rule: DrcRule, path: str) -> dict:
    monkeypatch.setattr(drc, "get_deck", lambda _: [rule])
    return drc.run_drc(path, "sky130")


def test_flush_edge_violation_polygon_is_two_point_segment(tmp_path, monkeypatch):
    """Zero-extension violations collapse to the failing edge's extremes.

    Pre-#5869 each edge pair reported the degenerate quad
    ``[[0,50],[0,1900],[0,100],[0,1950]]`` (point 4 duplicating point 2,
    point 3 interior to the 1-2 segment, area 0), mirrored for the right
    edge. Now each reports two distinct points spanning the full failing
    edge; ``bbox`` is unchanged -- it was already the edge's full extent.
    """
    rule = DrcRule(
        id="test.enclosing",
        description="synthetic 50 dbu enclosing",
        check="enclosing",
        layer=(1, 0),
        other_layer=(2, 0),
        threshold_dbu=50,
    )
    report = _run_with_deck(
        monkeypatch,
        rule,
        _write_two_layer_layout(tmp_path, REPRO_A, REPRO_B),
    )

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"test.enclosing": 2}
    by_left = {v["bbox"]["left"]: v for v in report["violations"]}
    assert set(by_left) == {0, 1000}
    assert by_left[0]["bbox"] == {
        "left": 0,
        "bottom": 50,
        "right": 0,
        "top": 1950,
    }
    assert by_left[0]["polygon"] == [[0, 50], [0, 1950]]
    assert by_left[1000]["bbox"] == {
        "left": 1000,
        "bottom": 50,
        "right": 1000,
        "top": 1950,
    }
    assert by_left[1000]["polygon"] == [[1000, 50], [1000, 1950]]


def test_positive_extension_violation_polygon_is_perimeter_order(tmp_path, monkeypatch):
    """A positive-extension enclosing violation is a real quad, in order.

    B inset by 20 dbu (< the 50 dbu threshold) leaves each edge pair a
    real quadrilateral with nonzero area. Pre-#7979 its hull was KLayout's
    same-direction walk -- ``[[0,54],[0,1946],[20,100],[20,1900]]`` -- a
    bowtie whose second side is a diagonal; the corners are unchanged, now
    walked around the perimeter. The two edges differ in length (1892 vs
    1800 dbu), so the crossed lobes have unequal area.
    """
    rule = DrcRule(
        id="test.enclosing",
        description="synthetic 50 dbu enclosing",
        check="enclosing",
        layer=(1, 0),
        other_layer=(2, 0),
        threshold_dbu=50,
    )
    report = _run_with_deck(
        monkeypatch,
        rule,
        _write_two_layer_layout(tmp_path, REPRO_A, (20, 100, 980, 1900)),
    )

    assert report["rule_counts"] == {"test.enclosing": 2}
    by_left = {v["bbox"]["left"]: v for v in report["violations"]}
    assert by_left[0]["polygon"] == [[0, 54], [0, 1946], [20, 1900], [20, 100]]
    assert by_left[980]["polygon"] == [
        [1000, 54],
        [980, 100],
        [980, 1900],
        [1000, 1946],
    ]
    assert_simple_quad(
        by_left[0]["polygon"], [[0, 54], [0, 1946], [20, 100], [20, 1900]]
    )
    assert_simple_quad(
        by_left[980]["polygon"], [[1000, 54], [980, 1900], [980, 100], [1000, 1946]]
    )


def test_equal_length_edges_violation_polygon_is_perimeter_order(tmp_path, monkeypatch):
    """Equal-length, positively-separated edges report the full rectangle.

    A pair whose two edges have the *same* length but real separation --
    B inset 20 dbu in x, flush in y -- is a genuine 2D marker region
    (``x ∈ [0,20]``, ``y ∈ [0,2000]``). Its raw hull is a *crossed* (bowtie)
    quad whose two lobes cancel exactly under a shoelace sum: zero signed
    area without collinearity. Collapsing it fabricates geometry (the
    diagonal of the marker region) or drops the enclosing edge outright
    (issue #5869), so it is not collapsed; it is re-walked around the
    rectangle's perimeter instead (issue #7979).
    """
    rule = DrcRule(
        id="test.enclosing",
        description="synthetic 50 dbu enclosing",
        check="enclosing",
        layer=(1, 0),
        other_layer=(2, 0),
        threshold_dbu=50,
    )
    report = _run_with_deck(
        monkeypatch,
        rule,
        _write_two_layer_layout(tmp_path, REPRO_A, (20, 0, 980, 2000)),
    )

    assert report["rule_counts"] == {"test.enclosing": 4}
    by_bbox = {
        (
            v["bbox"]["left"],
            v["bbox"]["bottom"],
            v["bbox"]["right"],
            v["bbox"]["top"],
        ): v
        for v in report["violations"]
    }
    # Left/right pairs: 20 dbu apart -- real rectangles, perimeter order.
    assert by_bbox[(0, 0, 20, 2000)]["polygon"] == [
        [0, 0],
        [0, 2000],
        [20, 2000],
        [20, 0],
    ]
    assert by_bbox[(980, 0, 1000, 2000)]["polygon"] == [
        [980, 0],
        [980, 2000],
        [1000, 2000],
        [1000, 0],
    ]
    for key in ((0, 0, 20, 2000), (980, 0, 1000, 2000)):
        left, bottom, right, top = key
        assert_simple_quad(
            by_bbox[key]["polygon"],
            [[left, bottom], [left, top], [right, bottom], [right, top]],
        )
    # Top/bottom pairs: flush (zero extension) -- genuinely collinear,
    # still collapse to the failing edge's extremes.
    assert by_bbox[(0, 0, 1000, 0)]["polygon"] == [[0, 0], [1000, 0]]
    assert by_bbox[(0, 2000, 1000, 2000)]["polygon"] == [[0, 2000], [1000, 2000]]


def test_space_check_violation_polygon_unchanged(tmp_path, monkeypatch):
    """``space`` markers -- the already-good shape -- are untouched.

    The gap quad between two same-layer shapes closer than the threshold
    is byte-identical to the pre-#5869 output.
    """
    rule = DrcRule(
        id="test.space",
        description="synthetic 50 dbu space",
        check="space",
        layer=(1, 0),
        threshold_dbu=50,
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 500, 1000))
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(540, 0, 1000, 1000))
    path = tmp_path / "space.gds"
    layout.write(str(path))
    report = _run_with_deck(monkeypatch, rule, str(path))

    assert report["rule_counts"] == {"test.space": 1}
    (violation,) = report["violations"]
    assert violation["polygon"] == [[500, 0], [500, 1000], [540, 1000], [540, 0]]


# --- unit: _edge_pair_hull_points ------------------------------------------


def test_hull_points_single_point_marker_is_null():
    """A fully degenerate single-point pair yields an empty hull -- the
    issue #2642 ``ongrid`` shape -- and stays ``null``, never a segment."""
    edge_pair = kdb.EdgePair(
        kdb.Edge(kdb.Point(1503, 0), kdb.Point(1503, 0)),
        kdb.Edge(kdb.Point(1503, 300), kdb.Point(1503, 300)),
    )
    assert _edge_pair_hull_points(edge_pair) is None


def test_hull_points_coincident_edges_collapse_to_segment():
    """Two coincident edges (zero separation on both sides of the pair)
    produce a collinear, duplicated-point hull; it collapses to the two
    extreme points without losing the edge's extent."""
    edge_pair = kdb.EdgePair(
        kdb.Edge(kdb.Point(0, 0), kdb.Point(0, 100)),
        kdb.Edge(kdb.Point(0, 0), kdb.Point(0, 100)),
    )
    assert _edge_pair_hull_points(edge_pair) == [[0, 0], [0, 100]]


def test_hull_points_antiparallel_quad_untouched():
    """An already-simple hull passes through with KLayout's vertex
    sequence verbatim -- here an antiparallel pair, which is what a
    ``space``/``width``/``separation`` marker is (issue #7979 must not
    reorder it, nor flip its winding)."""
    edge_pair = kdb.EdgePair(
        kdb.Edge(kdb.Point(0, 54), kdb.Point(0, 1946)),
        kdb.Edge(kdb.Point(20, 1900), kdb.Point(20, 100)),
    )
    assert _edge_pair_hull_points(edge_pair) == [
        [0, 54],
        [0, 1946],
        [20, 1900],
        [20, 100],
    ]


def test_hull_points_same_direction_quad_walked_in_perimeter_order():
    """A same-direction pair (what ``enclosed``/``enclosing`` produce) of
    *unequal* length: KLayout's raw hull is ``[[0,54],[0,1946],[20,100],
    [20,1900]]``, whose second side is a diagonal. Same four corners, now
    around the perimeter -- the sequence an antiparallel pair of the same
    two edges yields (see the test above)."""
    edge_pair = kdb.EdgePair(
        kdb.Edge(kdb.Point(0, 54), kdb.Point(0, 1946)),
        kdb.Edge(kdb.Point(20, 100), kdb.Point(20, 1900)),
    )
    points = _edge_pair_hull_points(edge_pair)
    assert points == [[0, 54], [0, 1946], [20, 1900], [20, 100]]
    assert_simple_quad(points, [[0, 54], [0, 1946], [20, 100], [20, 1900]])


def test_hull_points_equal_length_edges_walked_in_perimeter_order():
    """Equal-length, positively-separated edges make a *crossed* (bowtie)
    hull: its two lobes cancel exactly under a shoelace sum -- zero signed
    area without collinearity -- so it must not be collapsed. Regression
    control for the pre-#5869 zero-area predicate, which turned this pair
    into the bogus diagonal ``[[0,0],[20,2000]]``; the rectangle is now
    walked around its perimeter instead of along both diagonals
    (issue #7979)."""
    edge_pair = kdb.EdgePair(
        kdb.Edge(kdb.Point(0, 0), kdb.Point(0, 2000)),
        kdb.Edge(kdb.Point(20, 0), kdb.Point(20, 2000)),
    )
    points = _edge_pair_hull_points(edge_pair)
    assert points == [[0, 0], [0, 2000], [20, 2000], [20, 0]]
    assert_simple_quad(points, [[0, 0], [20, 2000], [20, 0], [0, 2000]])


def test_parallel_edge_pairs_walk_a_simple_ring_over_the_raw_corners():
    """Seeded property (issue #7979) over parallel edge pairs -- the shapes
    the checks emit: same or opposite direction, equal or unequal length,
    any offset and overlap, axis-aligned and 45 degree. For every pair that
    survives the #5869 collapses (empty -> ``null``, collinear -> segment):
    a raw hull that is already a simple ring is returned verbatim, and a
    crossed one comes back as a simple ring over the *same* corners. This
    is what the ``_edge_pair_hull_points`` comment claims for parallel
    pairs, and it pins KLayout's ``EdgePair.normalized()`` behaviour
    against a future upgrade."""
    rng = random.Random(7979)
    crossed = verbatim = 0
    for _ in range(3000):
        dx, dy = rng.choice([(1, 0), (0, 1), (1, 1), (1, -1)])
        offset = rng.randint(-5, 5)

        def at(u, o, dx=dx, dy=dy):
            return kdb.Point(dx * u - dy * o, dy * u + dx * o)

        edge_pair = kdb.EdgePair(
            kdb.Edge(at(rng.randint(-6, 6), 0), at(rng.randint(-6, 6), 0)),
            kdb.Edge(at(rng.randint(-6, 6), offset), at(rng.randint(-6, 6), offset)),
        )
        try:
            raw = [[p.x, p.y] for p in edge_pair.polygon(0).each_point_hull()]
        except Exception:
            raw = []
        points = _edge_pair_hull_points(edge_pair)
        if not raw:
            assert points is None
            continue
        if points is None or len(points) == 2:
            continue  # the #5869 collapses; covered by the tests above
        assert {tuple(p) for p in points} == {tuple(p) for p in raw}, (edge_pair, raw)
        assert first_meeting_sides(points) is None, (edge_pair, raw, points)
        if first_meeting_sides(raw) is None:
            assert points == raw, (edge_pair, raw, points)
            verbatim += 1
        else:
            crossed += 1
    # Not vacuous: both branches were exercised many times.
    assert crossed > 300 and verbatim > 300, (crossed, verbatim)


def test_ring_oracle_rejects_the_pre_fix_bowties():
    """The oracle has teeth: every ring the issue reports (and the two
    ``test_hull_points_*`` bowties above, one with cancelling lobes and one
    without) is rejected, so the structural assertions cannot pass
    vacuously."""
    bowties = [
        [[-110, -110], [-110, 1110], [0, 0], [0, 1000]],
        [[-110, -110], [2000, 0], [0, 0], [2110, -110]],
        [[0, 1000], [2000, 1000], [-110, 1110], [2110, 1110]],
        [[2110, -110], [2000, 1000], [2000, 0], [2110, 1110]],
        [[0, 54], [0, 1946], [20, 100], [20, 1900]],
        [[0, 0], [20, 2000], [20, 0], [0, 2000]],
    ]
    for ring in bowties:
        with pytest.raises(AssertionError):
            assert_simple_quad(ring)
    # ...and accepts a perimeter, including one traced the other way round.
    assert_simple_quad([[0, 0], [0, 2000], [20, 2000], [20, 0]])
    assert_simple_quad([[0, 0], [20, 0], [20, 2000], [0, 2000]])


def test_retraced_normalized_ring_keeps_the_three_extreme_points():
    # The last normalized vertex lies one dbu up the first side.
    pair = kdb.EdgePair(
        kdb.Edge(2000, 1000, 2000, 512), kdb.Edge(2100, 1100, 2000, 513)
    )
    points = _edge_pair_hull_points(pair)
    assert points is not None and len(points) == 3
    assert {tuple(point) for point in points} == {
        (2000, 512),
        (2000, 1000),
        (2100, 1100),
    }


def test_crossing_measurement_edges_keep_all_four_extreme_points():
    pair = kdb.EdgePair(kdb.Edge(0, 0, 20, 20), kdb.Edge(0, 20, 20, 0))
    assert_simple_quad(
        _edge_pair_hull_points(pair), [(0, 0), (20, 20), (0, 20), (20, 0)]
    )
