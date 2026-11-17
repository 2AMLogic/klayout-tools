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
"""

from __future__ import annotations

import klayout.db as kdb

import klayout_tools.drc as drc
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


def test_positive_extension_violation_polygon_unchanged(tmp_path, monkeypatch):
    """A positive-extension enclosing violation keeps its quad verbatim.

    B inset by 20 dbu (< the 50 dbu threshold) leaves each edge pair a
    real quadrilateral with nonzero area; its hull is byte-identical to
    the pre-#5869 output.
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
    assert by_left[0]["polygon"] == [[0, 54], [0, 1946], [20, 100], [20, 1900]]
    assert by_left[980]["polygon"] == [
        [1000, 54],
        [980, 1900],
        [980, 100],
        [1000, 1946],
    ]


def test_equal_length_edges_violation_polygon_unchanged(tmp_path, monkeypatch):
    """Equal-length, positively-separated edges keep their bowtie quad.

    A pair whose two edges have the *same* length but real separation --
    B inset 20 dbu in x, flush in y -- is a genuine 2D marker region
    (``x ∈ [0,20]``, ``y ∈ [0,2000]``). Its hull is a *crossed* (bowtie)
    quad whose two lobes cancel exactly under a shoelace sum: zero signed
    area without collinearity. Collapsing it fabricates geometry (the
    diagonal of the marker region) or drops the enclosing edge outright;
    it must pass through byte-identical to the pre-#5869 output.
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
    # Left/right pairs: 20 dbu apart -- real quadrilaterals, verbatim.
    assert by_bbox[(0, 0, 20, 2000)]["polygon"] == [
        [0, 0],
        [20, 2000],
        [20, 0],
        [0, 2000],
    ]
    assert by_bbox[(980, 0, 1000, 2000)]["polygon"] == [
        [980, 0],
        [980, 2000],
        [1000, 0],
        [1000, 2000],
    ]
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


def test_hull_points_positive_area_quad_untouched():
    """A proper quadrilateral hull passes through verbatim."""
    edge_pair = kdb.EdgePair(
        kdb.Edge(kdb.Point(0, 54), kdb.Point(0, 1946)),
        kdb.Edge(kdb.Point(20, 100), kdb.Point(20, 1900)),
    )
    assert _edge_pair_hull_points(edge_pair) == [
        [0, 54],
        [0, 1946],
        [20, 100],
        [20, 1900],
    ]


def test_hull_points_equal_length_edges_bowtie_hull_untouched():
    """Equal-length, positively-separated edges make a *crossed* (bowtie)
    hull: its two lobes cancel exactly under a shoelace sum -- zero signed
    area without collinearity -- so it must not be collapsed. Regression
    control for the pre-fix zero-area predicate, which turned this pair
    into the bogus diagonal ``[[0,0],[20,2000]]``."""
    edge_pair = kdb.EdgePair(
        kdb.Edge(kdb.Point(0, 0), kdb.Point(0, 2000)),
        kdb.Edge(kdb.Point(20, 0), kdb.Point(20, 2000)),
    )
    assert _edge_pair_hull_points(edge_pair) == [
        [0, 0],
        [20, 2000],
        [20, 0],
        [0, 2000],
    ]
