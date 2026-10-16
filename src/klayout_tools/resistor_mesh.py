"""Shared rectilinear-mesh geometry helpers: decompose a merged Manhattan
polygon into a grid of rectangular cells, and scope a via landing to the
rail polygons it physically touches.

These were private helpers of :mod:`klayout_tools.power` (issues #2171,
#2259); they live here so the extraction resistance-graph builder
(:mod:`klayout_tools.extract_resistance_graph`, issue #2980, Stage 3.D of
``docs/design/extract-fidelity-roadmap.md``) can reuse them without importing
``klt power``. The moved functions are unchanged except that
:func:`_decomposition_grid` / :func:`_decompose_manhattan_polygon` take
optional extra cut coordinates, defaulting to none, which reproduces the
original vertex grid exactly.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb


def _polygon_cut_coordinates(polygon: kdb.Polygon) -> tuple[list[int], list[int]]:
    """The sorted, deduplicated x/y coordinates of every vertex of a merged
    polygon (hull **and** holes) -- the cut lines of the rectangular grid
    :func:`_decompose_manhattan_polygon` carves it into."""
    xs: set[int] = set()
    ys: set[int] = set()
    for point in polygon.each_point_hull():
        xs.add(point.x)
        ys.add(point.y)
    for hole in range(polygon.holes()):
        for point in polygon.each_point_hole(hole):
            xs.add(point.x)
            ys.add(point.y)
    return sorted(xs), sorted(ys)


def _decomposition_grid(
    polygon: kdb.Polygon,
    max_cells: int,
    extra_x: Iterable[int] = (),
    extra_y: Iterable[int] = (),
) -> tuple[list[int] | None, list[int] | None, str | None]:
    """``(xs, ys, None)`` for a polygon worth decomposing, or ``(None, None,
    reason)`` when it is degenerate or would need more mesh cells than
    ``max_cells``.

    ``extra_x`` / ``extra_y`` are optional additional cut coordinates (dbu).
    Only those strictly inside the polygon's own vertex extent are added, so
    a coordinate that cannot cut it neither adds cells nor changes the grid;
    with none given the grid is exactly the vertex grid."""
    xs, ys = _polygon_cut_coordinates(polygon)
    if xs and ys:
        xs = sorted({*xs, *(x for x in extra_x if xs[0] < x < xs[-1])})
        ys = sorted({*ys, *(y for y in extra_y if ys[0] < y < ys[-1])})
    if len(xs) < 2 or len(ys) < 2:
        return None, None, "it has no positive-area extent"
    cells = (len(xs) - 1) * (len(ys) - 1)
    if cells > max_cells:
        return (
            None,
            None,
            (
                f"decomposing it needs up to {cells} mesh cells, over this build's "
                f"{max_cells}-mesh-cell cap (MAX_DECOMPOSITION_CELLS)"
            ),
        )
    return xs, ys, None


def _decompose_manhattan_polygon(
    polygon: kdb.Polygon,
    max_cells: int,
    extra_x: Iterable[int] = (),
    extra_y: Iterable[int] = (),
) -> tuple[list[tuple[int, int, kdb.Box]] | None, str | None]:
    """Cut one merged, axis-aligned polygon into the grid of rectangles
    formed by extending every vertex's x and y coordinate across it (issue
    #2171).

    Returns ``(cells, None)`` -- each cell a ``(column, row, box)`` triple
    keyed to that grid, so two cells sharing a boundary always share the
    *whole* side -- or ``(None, reason)`` when the polygon cannot be
    decomposed exactly (not Manhattan, degenerate, or over the cell cap).
    A caller must never fall back to the bounding box on a ``reason``: that
    is the non-conservative approximation this function exists to replace.

    ``extra_x`` / ``extra_y`` (dbu, default none) add cut lines at port
    positions -- a landing or terminal edge -- so a port always lies on a
    cell boundary (see :func:`_decomposition_grid`). With none the result is
    the plain vertex grid ``klt power`` consumes.
    """
    import klayout.db as kdb

    xs, ys, reason = _decomposition_grid(polygon, max_cells, extra_x, extra_y)
    if xs is None or ys is None:
        return None, reason
    columns = {x: index for index, x in enumerate(xs)}
    poly_region = kdb.Region(polygon)
    cells: list[tuple[int, int, kdb.Box]] = []
    for row in range(len(ys) - 1):
        band = kdb.Region(kdb.Box(xs[0], ys[row], xs[-1], ys[row + 1])) & poly_region
        for part in band.merged().each():
            if not part.is_box():
                return None, (
                    "it is not axis-aligned (Manhattan), so it has no exact "
                    "decomposition into rectangles"
                )
            box = part.bbox()
            left, right = columns.get(box.left), columns.get(box.right)
            if left is None or right is None:
                return None, "its rectangles do not land on its own vertex grid"
            for column in range(left, right):
                cells.append(
                    (
                        column,
                        row,
                        kdb.Box(xs[column], ys[row], xs[column + 1], ys[row + 1]),
                    )
                )
    if not cells:
        return None, "it has no positive-area extent"
    return cells, None


def _rails_under_via(
    rails: list[dict[str, Any]], via_region: kdb.Region, via_box: kdb.Box
) -> list[dict[str, Any]]:
    """The rails (merged polygons of one metal role, as recorded by
    :func:`_build_island_network`) that the via shape actually lands on --
    the scope a via tap's nearest-endpoint search is restricted to (issue
    #2259).

    Searching the whole net's rails instead is not a positional
    approximation, it is a connectivity error: on any trunk-and-stub rail (a
    PDN strap with drop-downs, a long rail with per-row risers) a via tapping
    the trunk partway along is nearer some unrelated short stub's endpoint
    than either of the trunk's own ends, so the tap is wired to the stub --
    severing the trunk from everything it feeds and shorting two unrelated
    segments together. The island then fragments into disconnected
    components, and the IR solve reports ``no_pad`` on the orphans while
    ``worst_case_droop_mv`` reads 0.

    Merged polygons on one layer are disjoint, so this is normally exactly
    one rail; a via shape spanning two of them physically bridges both, and
    both is still the correct scope. Touching counts as landing on: a via
    abutting a rail edge-to-edge is what joined them into this net."""
    hits: list[dict[str, Any]] = []
    for rail in rails:
        if not rail["bbox"].touches(via_box):
            continue
        if not rail["region"].interacting(via_region).is_empty():
            hits.append(rail)
    return hits
