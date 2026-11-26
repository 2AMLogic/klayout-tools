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
    outline vertices of the marker spanning its two edges in perimeter
    order, or ``None`` when the marker has no renderable outline.

    ``EdgePair.polygon(0)`` walks ``first.p1 -> first.p2 -> second.p1 ->
    second.p2``. That is the quad's perimeter only when the two edges are
    antiparallel (a ``space``/``width``/``separation`` pair); an
    ``enclosed``/``enclosing`` pair runs both edges the same direction, so
    the walk crosses itself (issue #7979). A crossed ring is re-walked from
    ``EdgePair.normalized()``, KLayout's own re-orientation of the pair for
    polygon conversion. Parallel pairs retain all four corners. A chamfer
    can instead yield a triangle when a collinear point drops out. If the
    normalized walk still crosses or retraces a side, its extreme points
    define the perimeter. A ring
    that is already simple keeps KLayout's vertex sequence verbatim.

    Three ways a marker has no usable outline, per the contract in
    ``docs/cli/drc.md`` (all judged on the *original* hull, before any
    re-walk -- normalizing first would turn the two-point segments below
    into ``null``):

    - ``EdgePair.polygon(0)`` raises (a degenerate pair KLayout declines to
      turn into a polygon at all) -- reported as ``null``, the long-standing
      case;
    - it returns a polygon whose hull is *empty*, which is what a fully
      degenerate single-point marker yields -- ``"ongrid"``'s per-vertex
      markers are exactly that (issue #2642). Normalized to ``None`` here
      rather than emitted as an empty array, which would be a third,
      undocumented shape for this field;
    - it returns a *collinear* hull -- every vertex on one line (the
      failing edge), with duplicated and interior points -- which is what a
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
    if _ring_self_intersects(points):
        # KLayout reorients the pair before walking its outline. At a
        # chamfer the polygon conversion can omit a collinear point.
        normalized = [
            [pt.x, pt.y] for pt in edge_pair.normalized().polygon(0).each_point_hull()
        ]
        if _ring_self_intersects(normalized):
            return _convex_perimeter(points)
        return normalized
    return points


def _turn(a: list[int], b: list[int], c: list[int]) -> int:
    """Signed cross product of a-b and a-c, in integer database units."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _convex_perimeter(points: list[list[int]]) -> list[list[int]]:
    """Walk the extreme vertices of a crossed four-point marker.

    The lower and upper chains turn left at every retained vertex. This
    removes a retraced collinear point while keeping both ends of the side.
    """

    def chain(vertices: list[list[int]]) -> list[list[int]]:
        result: list[list[int]] = []
        for vertex in vertices:
            while len(result) >= 2 and _turn(result[-2], result[-1], vertex) <= 0:
                result.pop()
            result.append(vertex)
        return result[:-1]  # The other chain starts at this endpoint.

    ordered = sorted(points)
    return chain(ordered) + chain(ordered[::-1])


def _ring_self_intersects(points: list[list[int]]) -> bool:
    """True when two non-adjacent sides of the closed ring ``points`` meet
    -- cross, touch, or overlap. Exact integer arithmetic on dbu
    coordinates, so there is no epsilon.

    This is a side-intersection test rather than a signed-area one for the
    same reason :func:`_hull_is_collinear` is not an area test: a crossed
    quad's two lobes cancel only when the edges are equal length, so area
    alone cannot tell a bowtie from a perimeter (issue #7979).
    """
    count = len(points)
    if count < 4:
        return False
    for i in range(count):
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue  # the closing side is adjacent to the first
            if _sides_meet(
                points[i],
                points[(i + 1) % count],
                points[j],
                points[(j + 1) % count],
            ):
                return True
    return False


def _sides_meet(a: list[int], b: list[int], c: list[int], d: list[int]) -> bool:
    """True when closed segments ``a-b`` and ``c-d`` share at least a point."""

    def _orient(p: list[int], q: list[int], r: list[int]) -> int:
        cross = _turn(p, q, r)
        return (cross > 0) - (cross < 0)

    def _within(p: list[int], q: list[int], r: list[int]) -> bool:
        """``r`` (already collinear with ``p-q``) lies inside its box."""
        in_x = min(p[0], q[0]) <= r[0] <= max(p[0], q[0])
        in_y = min(p[1], q[1]) <= r[1] <= max(p[1], q[1])
        return in_x and in_y

    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    return (
        (o1 == 0 and _within(a, b, c))
        or (o2 == 0 and _within(a, b, d))
        or (o3 == 0 and _within(c, d, a))
        or (o4 == 0 and _within(c, d, b))
    )


def _hull_is_collinear(points: list[list[int]]) -> bool:
    """True when every distinct vertex lies on one line. Exact integer
    cross products against a reference direction; dbu coordinates are
    ints, so there is no epsilon to worry about.

    This is deliberately *not* a zero-shoelace-area test: a raw
    ``EdgePair.polygon(0)`` hull can be a crossed (bowtie) quad (see
    :func:`_edge_pair_hull_points`) whose two lobes cancel exactly whenever
    the pair's two edges have equal length -- zero signed area without
    collinearity, which would collapse ordinary positive-separation 2D
    markers (issue #5901 review).
    """
    distinct: list[list[int]] = []
    for point in points:
        if point not in distinct:
            distinct.append(point)
    if len(distinct) < 3:
        return True
    (x0, y0), (x1, y1) = distinct[0], distinct[1]
    dx, dy = x1 - x0, y1 - y0
    return all((x - x0) * dy - (y - y0) * dx == 0 for x, y in distinct[2:])


def _collapsed_segment(points: list[list[int]]) -> list[list[int]] | None:
    """Deduplicate a collinear hull to its two extreme points, keeping the
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
