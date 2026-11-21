"""Source attribution for ``klt drc`` violations (per-instance ownership, and
the geometry-based grid-marker rule of issue #6447).

Split out of :mod:`klayout_tools.drc` to satisfy the per-file size ratchet, the
same way :mod:`klayout_tools.drc_layer_selection` was. It answers one question
for every recorded violation: *which placed instance owns it?* -- reported as
the ``"source_cell"`` / ``"source_path"`` pair (both ``None`` when no single
placement can be named; the caller's top-cell attribution is then the only
honest answer).

Two attribution rules live here:

- :func:`attribute_to_instance` -- the general rule: the innermost placement
  whose bounding box wholly contains the violation's ``bbox``. Right for a
  violation that sits *inside* one placement (a width or area fault), but only
  approximate for a vertex marker, which can land on a child's boundary
  without the child having drawn anything there.
- :func:`attribute_grid_marker` -- the rule for ``"ongrid"`` point markers on a
  physical layer (issue #6447). The check runs on the *merged* region, whose
  boundary gains T-vertices where one shape's edge crosses another's. A
  top-level shape with an off-grid edge overlapping an on-grid child therefore
  produced a marker on the child's boundary, and bbox containment named the
  child although the child drew nothing off-grid. The marker is instead
  attributed to the placement whose *own source edges* supply the offending
  coordinate(s) at that point, and to no one when that evidence is absent,
  conflicting, or unprovable.

Neither rule touches detection: the merged region stays the source of truth
for *what* is reported; this module only decides who it is filed under.
"""

from __future__ import annotations

from typing import Any

from .drc_check_kinds import _GRID_CHECKS

#: ``(source_cell, source_path)`` -- both ``None`` when unattributed.
Attribution = tuple[str | None, list[str] | None]

#: One placed occurrence of a shape's owning cell: the cell-name chain from the
#: checked cell's direct child down to the cell holding the shape (empty for a
#: shape drawn in the checked cell itself), and the accumulated transform that
#: pins *which* placement of that chain it is. Names alone cannot tell two
#: placements of one definition apart; the transform can.
_Origin = tuple[tuple[str, ...], str]

#: Per axis (``"x"``: supplied by a vertical edge, ``"y"``: by a horizontal
#: one), the origins owning a source edge through the marker at that coordinate.
_Evidence = dict[str, set[_Origin]]


def attribute_to_instance(top_cell: Any, bbox: Any) -> Attribution:
    """Map a violation ``bbox`` (in ``top_cell`` coordinates) back to the
    innermost placed instance whose own bounding box fully contains it.

    Returns a ``(source_cell, source_path)`` pair. ``source_cell`` is the
    cell-definition name of that innermost instance and ``source_path`` is
    the chain of cell names from ``top_cell``'s direct child down to it
    (inclusive). Both are ``None`` when no single placed instance contains
    ``bbox`` -- i.e. the violation is top-level geometry, or its bbox
    straddles an instance boundary (a spacing/enclosure violation *between*
    two placements belongs to neither), so the caller's top-cell attribution
    is the only honest answer.

    This never touches the flattened geometry the check actually ran on: it
    is a pure, additive lookup over the instance tree, spatially restricted
    to ``bbox`` via ``Cell.begin_instances_rec_touching`` so it stays cheap
    even for macros with hundreds of thousands of placements. The instance
    tree is walked depth-first by KLayout; among every candidate whose world
    bbox contains the violation, the *deepest* one (longest instance path)
    wins, with a deterministic tie-break (smallest world-bbox area, then
    lexicographic path) so repeated runs and different KLayout builds agree.
    """
    best_depth = -1
    best_area: int | None = None
    best_path: list[str] | None = None

    it = top_cell.begin_instances_rec_touching(bbox)
    while not it.at_end():
        inst_cell = it.inst_cell()
        world = (it.trans() * it.inst_trans()) * inst_cell.bbox()
        # Full containment (not mere touching): the violation must sit wholly
        # inside this placement for it to be the origin. `begin_..._touching`
        # over-returns edge-touching neighbours, so filter explicitly.
        if (
            world.left <= bbox.left
            and world.bottom <= bbox.bottom
            and world.right >= bbox.right
            and world.top >= bbox.top
        ):
            path = [ie.inst().cell.name for ie in it.path()]
            path.append(inst_cell.name)
            depth = len(path)
            area = world.width() * world.height()
            if (
                depth > best_depth
                or (depth == best_depth and best_area is not None and area < best_area)
                or (
                    depth == best_depth
                    and best_area is not None
                    and area == best_area
                    and best_path is not None
                    and path < best_path
                )
            ):
                best_depth = depth
                best_area = area
                best_path = path
        it.next()

    if best_path is None:
        return None, None
    return best_path[-1], best_path


def attribute_violation(rule: Any, cell: Any, bbox: Any) -> Attribution:
    """The ``(source_cell, source_path)`` for one violation of ``rule`` found
    under the checked ``cell``: the scoped geometry rule for a native
    physical-layer ``"ongrid"`` point marker (:func:`attribute_grid_marker`),
    bbox containment (:func:`attribute_to_instance`) for everything else."""
    if _is_scoped_grid_marker(rule, bbox):
        return attribute_grid_marker(rule, cell, bbox.left, bbox.bottom)
    return attribute_to_instance(cell, bbox)


def _is_scoped_grid_marker(rule: Any, bbox: Any) -> bool:
    """Whether ``bbox`` is the single-point marker of a native ``"ongrid"``
    rule on a physical layer -- the only shape this module's geometry rule
    covers. A ``derived_layer`` rule checks a region built by boolean
    derivation rather than a drawn layer, so its markers have no source edge
    on ``rule.layer`` to inspect (derived-layer ownership is out of scope);
    a non-point ``bbox`` is not a vertex marker."""
    return (
        rule.check in _GRID_CHECKS
        and rule.derived_layer is None
        and bbox.left == bbox.right
        and bbox.bottom == bbox.top
    )


def attribute_grid_marker(rule: Any, cell: Any, x: int, y: int) -> Attribution:
    """Attribute the ``"ongrid"`` marker at ``(x, y)`` (checked-cell dbu) to
    the one placement whose own source edges supply its offending
    coordinate(s), else to no one.

    Both fields are ``None`` together, or are the existing cell name and
    instance-name path types. A top-level origin (the marker is owed to
    geometry drawn in the checked cell itself) is ``None``, exactly as before.
    """
    origin = _grid_marker_origin(rule, cell, x, y)
    if origin is None or not origin[0]:
        return None, None
    path = list(origin[0])
    return path[-1], path


def _grid_dbu(rule: Any, dbu: float) -> int | None:
    """``rule.grid_um`` in whole database units -- the integer grid
    ``_run_grid_check`` handed ``Region.grid_check`` -- or ``None`` when the
    rule has no grid or it is finer than one database unit (every coordinate
    is then on-grid and no marker exists to attribute)."""
    if rule.grid_um is None:
        return None
    grid = round(rule.grid_um / dbu)
    return grid if grid >= 1 else None


def _grid_marker_origin(rule: Any, cell: Any, x: int, y: int) -> _Origin | None:
    """The single :data:`_Origin` owning the marker at ``(x, y)``, or ``None``
    when the evidence does not name exactly one."""
    layout = cell.layout()
    layer_index = layout.find_layer(*rule.layer)
    grid = _grid_dbu(rule, layout.dbu)
    if layer_index is None or grid is None:
        return None
    evidence = _edge_evidence(layout, cell, layer_index, x, y)
    if evidence is None:
        return None
    axes = [axis for axis, value in (("x", x), ("y", y)) if value % grid != 0]
    return _single_origin(evidence, axes)


def _single_origin(evidence: _Evidence, axes: list[str]) -> _Origin | None:
    """The one origin supplying *every* offending axis, else ``None``.

    An offending axis with no source edge is incomplete evidence; two or more
    origins -- across axes (an edge from one placement, the crossing
    coordinate from another) or within one (overlapping placements both
    drawing an edge there) -- are conflicting evidence. Neither is guessed at.
    """
    if not axes or any(not evidence[axis] for axis in axes):
        return None
    origins = set().union(*(evidence[axis] for axis in axes))
    if len(origins) != 1:
        return None
    return next(iter(origins))


def _edge_evidence(
    layout: Any, cell: Any, layer_index: int, x: int, y: int
) -> _Evidence | None:
    """Source edges through the point ``(x, y)`` on ``layer_index``, grouped by
    the axis each one supplies and the placement that owns it.

    The shape search is spatially restricted to the point, so it stays cheap
    on a chip-scale hierarchy; shapes are taken in checked-cell coordinates
    through their full instance transform, i.e. exactly as the checked region
    read them. ``None`` means the geometry at the point cannot be proven
    (a non-Manhattan edge passes through it): the caller treats that as
    unattributed rather than reading evidence off a partial picture.
    """
    import klayout.db as kdb

    evidence: _Evidence = {"x": set(), "y": set()}
    it = kdb.RecursiveShapeIterator(layout, cell, layer_index, kdb.Box(x, y, x, y))
    while not it.at_end():
        polygon = it.shape().polygon  # `None` for text and other non-polygons
        if polygon is not None:
            origin = (
                tuple(ie.inst().cell.name for ie in it.path()),
                it.trans().to_s(),
            )
            if not _add_edge_evidence(
                evidence, polygon.transformed(it.trans()), origin, x, y
            ):
                return None
        it.next()
    return evidence


def _add_edge_evidence(
    evidence: _Evidence, polygon: Any, origin: _Origin, x: int, y: int
) -> bool:
    """Record ``origin`` for each edge of ``polygon`` through ``(x, y)``: a
    horizontal edge supplies the Y coordinate, a vertical one the X. Returns
    ``False`` on a diagonal edge through the point -- diagonal attribution is
    out of scope, so the point's ownership is unprovable."""
    for edge in polygon.each_edge():
        if not _edge_contains(edge, x, y):
            continue
        if edge.dx() == 0 and edge.dy() == 0:
            continue  # a zero-length edge supplies no coordinate
        if edge.dy() == 0:
            evidence["y"].add(origin)
        elif edge.dx() == 0:
            evidence["x"].add(origin)
        else:
            return False
    return True


def _edge_contains(edge: Any, x: int, y: int) -> bool:
    """Whether ``(x, y)`` lies on the closed segment of ``edge``: endpoints
    count, because a source *corner* is exactly where an edge's own offending
    coordinate becomes a vertex."""
    p1, p2 = edge.p1, edge.p2
    return (
        min(p1.x, p2.x) <= x <= max(p1.x, p2.x)
        and min(p1.y, p2.y) <= y <= max(p1.y, p2.y)
        and (p2.x - p1.x) * (y - p1.y) == (p2.y - p1.y) * (x - p1.x)
    )
