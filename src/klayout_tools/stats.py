"""Compute area/density/polygon/vertex statistics for a GDSII/OASIS stream.

Pure library: :func:`stats_report` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints. Serialisation and human-readable
formatting live in the CLI command module so this function stays reusable (e.g.
by a future MCP server).

Headless invariant: uses the pip ``klayout`` package's batch database API
(``klayout.db``) only — no GUI, no Qt. Runnable in CI.

Determinism: every count and area figure is accumulated in database units
(exact Python ``int`` arithmetic) and converted to micrometres with a single
final multiplication, so results do not depend on shape iteration order or
floating-point summation order — the same input always produces the same
output.
"""

from __future__ import annotations

from typing import Any

from ._annotation import is_reserved_annotation_layer
from ._layout import bbox_um_dict, cells_in_hierarchy, load_layout


class StatsError(Exception):
    """Raised when a layout file cannot be read or its statistics computed.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def _is_area_shape(shape: Any) -> bool:
    """True for shape kinds that carry drawn area/vertices (box/polygon/path).

    Excludes text labels, edges, and edge pairs, which are not drawn
    geometry.
    """
    return bool(shape.is_box() or shape.is_polygon() or shape.is_path())


def _shape_vertex_count(shape: Any) -> int:
    polygon = shape.polygon
    return polygon.num_points() if polygon is not None else 0


def _instance_weights(cells: list[Any]) -> dict[int, int]:
    """Instance multiplicity for each cell *definition* in ``cells``.

    Fixes issue #1105: a leaf cell placed via a single ``CellInstArray`` with
    N copies previously contributed its shape area to ``_accumulate`` exactly
    once (per cell *definition*), never multiplied by N, while the density
    denominator (the top cell's hierarchy-inclusive ``bbox()``) already spans
    every array position -- producing a near-zero density for
    array-instanced macros.

    This walks the instance graph restricted to ``cells`` (typically the
    scope ``_accumulate`` is about to sum over -- either the whole stream or
    one cell's hierarchy, see :func:`klayout_tools._layout.cells_in_hierarchy`)
    and computes, for every cell definition in that scope, the total number
    of times it is reached by walking instances from the scope's own
    root(s) -- weighting each ``CellInstArray``/``Instance`` edge by
    ``Instance.size()`` (``na * nb`` for an array, 1 for a single
    placement), and multiplying by the weight already accumulated on the
    parent so nested/repeated instantiation compounds correctly.

    A cell with no parent *within the scope* (the scope's own root -- e.g.
    the ``--top`` cell itself, or an orphan cell never instantiated anywhere
    in a whole-stream scope) gets weight 1: it is counted once as its own
    definition, matching the pre-#1105 convention for anything that isn't
    reached through a multiplying instance edge.

    The cell hierarchy is a DAG (KLayout does not allow instantiation
    cycles), so a depth-first postorder walk from every such root, reversed,
    is a valid topological order: every edge parent->child has parent
    appearing first. Accumulating weights in that order guarantees a cell's
    weight is fully resolved (every incoming edge counted) before it
    contributes to its own children -- including diamond-shaped hierarchies,
    where a cell is instantiated by more than one parent within scope.
    """
    scope_indices = {cell.cell_index() for cell in cells}
    index_to_cell = {cell.cell_index(): cell for cell in cells}
    children: dict[int, list[tuple[int, int]]] = {idx: [] for idx in scope_indices}
    in_degree: dict[int, int] = dict.fromkeys(scope_indices, 0)

    for idx in scope_indices:
        for inst in index_to_cell[idx].each_inst():
            child_idx = inst.cell_index
            if child_idx not in scope_indices:
                continue
            children[idx].append((child_idx, inst.size()))
            in_degree[child_idx] += 1

    roots = [idx for idx in scope_indices if in_degree[idx] == 0]

    visited: set[int] = set()
    order: list[int] = []

    def _visit(idx: int) -> None:
        stack = [(idx, iter(children[idx]))]
        visited.add(idx)
        while stack:
            node, edges = stack[-1]
            advanced = False
            for child_idx, _count in edges:
                if child_idx not in visited:
                    visited.add(child_idx)
                    stack.append((child_idx, iter(children[child_idx])))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()

    for root_idx in roots:
        _visit(root_idx)
    order.reverse()

    weights: dict[int, int] = dict.fromkeys(scope_indices, 0)
    for root_idx in roots:
        weights[root_idx] = 1
    for idx in order:
        weight = weights[idx]
        if weight == 0:
            continue
        for child_idx, count in children[idx]:
            weights[child_idx] += weight * count

    return weights


def _accumulate(
    cells: Any, layer_index: int, weights: dict[int, int]
) -> tuple[int, int, int]:
    """Sum (area_dbu2, polygon_count, vertex_count) for one layer.

    ``cells`` is the set of cell *definitions* to sum over -- every cell in
    the stream by default, or (with ``--top`` given, issue #554) just the
    named top cell's own hierarchy (itself plus every cell it calls, see
    :func:`klayout_tools._layout.cells_in_hierarchy`), so a scoped ``--top``
    report does not silently keep summing shapes drawn only in unrelated
    top-cell hierarchies.

    ``weights`` (issue #1105, see :func:`_instance_weights`) maps each
    cell's ``cell_index()`` to how many times it is instantiated within that
    scope. Only ``area_dbu2`` is multiplied by it: the density denominator
    (the scope's hierarchy-inclusive bounding box, see :func:`stats_report`)
    already spans every array position, so a leaf cell placed via an N-copy
    ``CellInstArray`` must contribute N times its own drawn area for
    ``density`` to mean anything. ``polygon_count``/``vertex_count`` stay
    per cell *definition* (each shape counted once where it is defined, not
    multiplied by instantiation) -- the shape-count convention ``klt
    layers`` also uses, deliberately left unchanged since #1105 is scoped to
    the area/density numerator, not shape counts. Overlapping shapes are
    **not** merged, so area may double-count overlapping geometry; this
    keeps the computation cheap and exactly reproducible.
    """
    area_dbu2 = 0
    polygon_count = 0
    vertex_count = 0
    for cell in cells:
        weight = weights.get(cell.cell_index(), 0)
        for shape in cell.shapes(layer_index).each():
            if not _is_area_shape(shape):
                continue
            area_dbu2 += weight * shape.area()
            polygon_count += 1
            vertex_count += _shape_vertex_count(shape)
    return area_dbu2, polygon_count, vertex_count


def _density(area_dbu2: int, bbox_area_dbu2: int) -> float:
    if bbox_area_dbu2 <= 0:
        return 0.0
    return area_dbu2 / bbox_area_dbu2


def stats_report(
    path: str, per_layer: bool = False, top: str | None = None
) -> dict[str, Any]:
    """Compute area/density/polygon/vertex statistics for a GDSII or OASIS stream.

    KLayout auto-detects the stream format on read, so both ``.gds`` and
    ``.oas`` inputs are handled by the same code path.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/stats.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "dbu_um": <database unit in micrometres, float>,
            "top_cell": <top cell name, or None if the layout has no cells>,
            "bbox_um": {"left": .., "bottom": .., "right": .., "top": ..,
                        "width": .., "height": ..},
            "total": {"area_um2": .., "density": .., "polygon_count": ..,
                       "vertex_count": ..},
            "layers": None | [
                {"layer": int, "datatype": int, "name": str | None,
                 "area_um2": float, "density": float,
                 "polygon_count": int, "vertex_count": int,
                 "annotation": bool},
                ...
            ],
        }

    ``bbox_um`` is the top cell's bounding box (hierarchy-inclusive, i.e. it
    covers instantiated sub-cells too), in micrometres. ``density`` — for
    both ``total`` and each ``layers[]`` entry — is drawn area divided by
    this same bbox area, so every density figure shares one reference frame.

    ``area_um2`` is weighted by instance multiplicity (issue #1105): a leaf
    cell's shapes are counted once for each time it is reached by walking
    instances from the reporting scope's own root (once for a plain
    placement, N times for an N-copy ``CellInstArray``), so ``density``
    reflects the layout's actual drawn occupancy rather than the sum of
    each cell *definition*'s own area regardless of how many times it is
    placed. ``polygon_count``/``vertex_count`` are **not** weighted this way
    -- they stay per cell definition (each shape counted once where it is
    defined), matching ``klt layers``' shape-count convention. Overlapping
    shapes are not merged, so area may double-count overlapping geometry.

    ``layers`` is ``None`` unless ``per_layer=True``, in which case it is a
    list sorted by ``(layer, datatype)`` ascending for deterministic output.
    Each entry's ``annotation`` is ``True`` when the layer number falls in
    the reserved annotation-layer range (GDS layers 990-999, any datatype --
    see ``docs/cli/drc.md`` -> "Reserved annotation layer"), matching ``klt
    layers``.

    ``top`` (issue #554) names the top cell to report on when the stream has
    more than one -- required in that case, optional otherwise (matching
    ``klt extract --top``'s semantics). When given, both ``bbox_um`` *and*
    every area/polygon/vertex count (``total`` and, if requested, each
    ``layers[]`` entry) are scoped to that cell's own hierarchy -- itself
    plus every cell it calls, directly or indirectly -- not the whole
    stream; omitting it preserves today's whole-layout accumulation
    (unchanged for a single-top-cell stream, since that hierarchy already is
    the whole stream). See :func:`klayout_tools._layout.cells_in_hierarchy`.

    Raises :class:`StatsError` if the file is missing, unreadable, not a
    recognisable layout stream, ``top`` names a cell absent from the stream,
    or (with ``top`` omitted) the stream has more than one top cell
    (ambiguous bounding-box reference).
    """
    layout = load_layout(path, StatsError)

    # Imported lazily (after load_layout, which already paid this cost) for
    # kdb.Box() below.
    import klayout.db as kdb

    dbu = layout.dbu

    if top is not None:
        top_cell = layout.cell(top)
        if top_cell is None:
            raise StatsError(f"top cell not found in stream: {top}")
        scope_cells: Any = cells_in_hierarchy(layout, top_cell)
    else:
        top_cells = list(layout.top_cells())
        if len(top_cells) > 1:
            names = ", ".join(sorted(c.name for c in top_cells))
            raise StatsError(
                f"layout '{path}' has multiple top cells ({names}); "
                "a single top cell is required for the bounding-box "
                "reference. Pass --top to select one."
            )
        top_cell = top_cells[0] if top_cells else None
        scope_cells = list(layout.each_cell())

    bbox = top_cell.bbox() if top_cell is not None else kdb.Box()
    bbox_area_dbu2 = 0 if bbox.empty() else bbox.width() * bbox.height()

    # Issue #1105: weight each cell definition's area by how many times a
    # CellInstArray instances it within this scope, so an N-copy array
    # contributes N times its leaf area to the density numerator -- matching
    # the hierarchy-inclusive bbox above, which already spans every copy.
    weights = _instance_weights(scope_cells)

    total_area_dbu2 = 0
    total_polygon_count = 0
    total_vertex_count = 0
    per_layer_entries: list[dict[str, Any]] | None = [] if per_layer else None

    for layer_index in layout.layer_indexes():
        area_dbu2, polygon_count, vertex_count = _accumulate(
            scope_cells, layer_index, weights
        )
        total_area_dbu2 += area_dbu2
        total_polygon_count += polygon_count
        total_vertex_count += vertex_count

        if per_layer_entries is not None:
            info = layout.get_info(layer_index)
            per_layer_entries.append(
                {
                    "layer": info.layer,
                    "datatype": info.datatype,
                    "name": info.name if info.name else None,
                    "area_um2": area_dbu2 * dbu * dbu,
                    "density": _density(area_dbu2, bbox_area_dbu2),
                    "polygon_count": polygon_count,
                    "vertex_count": vertex_count,
                    "annotation": is_reserved_annotation_layer(
                        info.layer, info.datatype
                    ),
                }
            )

    if per_layer_entries is not None:
        per_layer_entries.sort(key=lambda entry: (entry["layer"], entry["datatype"]))

    return {
        "schema_version": 1,
        "file": path,
        "dbu_um": dbu,
        "top_cell": top_cell.name if top_cell is not None else None,
        "bbox_um": bbox_um_dict(bbox, dbu),
        "total": {
            "area_um2": total_area_dbu2 * dbu * dbu,
            "density": _density(total_area_dbu2, bbox_area_dbu2),
            "polygon_count": total_polygon_count,
            "vertex_count": total_vertex_count,
        },
        "layers": per_layer_entries,
    }
