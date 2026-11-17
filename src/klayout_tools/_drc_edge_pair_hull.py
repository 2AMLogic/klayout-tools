"""``violations[].polygon`` hull shaping for ``klt drc`` edge-pair markers.

Split out of :mod:`klayout_tools.drc` (issue #5869) to satisfy the per-file
size ratchet: the zero-area-hull collapse logic grew past ``drc.py``'s
ceiling, so it lives here and is imported back. Behaviour is unchanged --
``_edge_pair_hull_points`` is the single entry point ``drc.py`` calls.
"""

from __future__ import annotations

from typing import Any


def _edge_pair_hull_points(edge_pair: Any) -> list[list[int]] | None:
    """``violations[].polygon`` for one ``EdgePair`` violation marker: the
    hull points of the quadrilateral spanning its two edges, or ``None`` when
    the marker has no renderable outline.

    Three ways a marker has no usable outline, per the contract in
    ``docs/cli/drc.md``:

    - ``EdgePair.polygon(0)`` raises (a degenerate pair KLayout declines to
      turn into a polygon at all) -- reported as ``null``, the long-standing
      case;
    - it returns a polygon whose hull is *empty*, which is what a fully
      degenerate single-point marker yields -- ``"ongrid"``'s per-vertex
      markers are exactly that (issue #2642). Normalized to ``None`` here
      rather than emitted as an empty array, which would be a third,
      undocumented shape for this field;
    - it returns a *zero-area* hull -- every vertex collinear on the
      failing edge, with duplicated and interior points -- which is what a
      total-deficit edge pair produces: an ``enclosing`` violation whose
      zero-extension edge is exactly coincident with its peer (issue
      #5869's repro collapses to ``[(0,50),(0,1900),(0,100),(0,1950)]``).
      Such a quad is not a simple polygon and carries no usable marker
      shape, so it is collapsed to the two extreme points of the failing
      edge (:func:`_collapsed_segment`), a documented 2-point segment.
    """
    try:
        points = [[pt.x, pt.y] for pt in edge_pair.polygon(0).each_point_hull()]
    except Exception:
        return None
    if not points:
        return None
    if _hull_is_collinear(points):
        return _collapsed_segment(points)
    return points


def _hull_is_collinear(points: list[list[int]]) -> bool:
    """True when the hull point ring has zero area -- every vertex on one
    line. Exact integer shoelace; dbu coordinates are ints, so there is no
    epsilon to worry about."""
    area2 = 0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        area2 += x1 * y2 - x2 * y1
    return area2 == 0


def _collapsed_segment(points: list[list[int]]) -> list[list[int]] | None:
    """Deduplicate a zero-area hull to its two extreme points, keeping the
    full extent along the edge (issue #5869). Returns ``None`` for the
    zero-extent corner case (every point identical) -- a marker with no
    extent in either direction has no renderable outline, the same
    verdict as the empty hull. Distinct collinear points always have
    distinct projections onto any edge direction, so the two extremes are
    distinct.
    """
    distinct: list[list[int]] = []
    for point in points:
        if point not in distinct:
            distinct.append(point)
    if len(distinct) < 2:
        return None
    dx = distinct[1][0] - distinct[0][0]
    dy = distinct[1][1] - distinct[0][1]

    def _projection(point: list[int]) -> int:
        return point[0] * dx + point[1] * dy

    return [min(distinct, key=_projection), max(distinct, key=_projection)]
