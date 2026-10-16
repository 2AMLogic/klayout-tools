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
) -> tuple[int, int, int, int, int]:
    """Sum (area_dbu2, polygon_count, vertex_count, flattened_polygon_count,
    flattened_vertex_count) for one layer.

    ``cells`` is the set of cell *definitions* to sum over -- every cell in
    the stream by default, or (with ``--top`` given, issue #554) just the
    named top cell's own hierarchy (itself plus every cell it calls, see
    :func:`klayout_tools._layout.cells_in_hierarchy`), so a scoped ``--top``
    report does not silently keep summing shapes drawn only in unrelated
    top-cell hierarchies.

    ``weights`` (issue #1105, see :func:`_instance_weights`) maps each
    cell's ``cell_index()`` to how many times it is instantiated within that
    scope. ``area_dbu2`` is multiplied by it: the density denominator
    (the scope's hierarchy-inclusive bounding box, see :func:`stats_report`)
    already spans every array position, so a leaf cell placed via an N-copy
    ``CellInstArray`` must contribute N times its own drawn area for
    ``density`` to mean anything. ``polygon_count``/``vertex_count`` stay
    per cell *definition* (each shape counted once where it is defined, not
    multiplied by instantiation) -- the shape-count convention ``klt
    layers`` also uses, deliberately left unchanged since #1105 is scoped to
    the area/density numerator, not shape counts.

    ``flattened_polygon_count``/``flattened_vertex_count`` (issue #2366) are
    the instance-resolved counterparts: the same per-shape polygon/vertex
    counts as ``polygon_count``/``vertex_count``, but weighted by ``weights``
    exactly like ``area_dbu2`` above, so a shape drawn once in a cell placed
    N times via a ``CellInstArray`` contributes N to these totals -- matching
    ``klt layers --flattened``'s ``flattened_shapes`` convention. Overlapping
    shapes are **not** merged here, so area and the flattened counts may
    double-count overlapping geometry (``merged_area_um2`` in
    :func:`stats_report` is the unioned figure).
    """
    area_dbu2 = 0
    polygon_count = 0
    vertex_count = 0
    flattened_polygon_count = 0
    flattened_vertex_count = 0
    for cell in cells:
        weight = weights.get(cell.cell_index(), 0)
        for shape in cell.shapes(layer_index).each():
            if not _is_area_shape(shape):
                continue
            shape_vertex_count = _shape_vertex_count(shape)
            area_dbu2 += weight * shape.area()
            polygon_count += 1
            vertex_count += shape_vertex_count
            flattened_polygon_count += weight
            flattened_vertex_count += weight * shape_vertex_count
    return (
        area_dbu2,
        polygon_count,
        vertex_count,
        flattened_polygon_count,
        flattened_vertex_count,
    )


#: Upper bound on windows per layer for ``--density-window``; a finer grid is
#: refused rather than emitting unbounded JSON.
MAX_DENSITY_WINDOWS = 10_000


def _merged_region(top_cell: Any, layer_index: int, kdb: Any) -> Any:
    """Flattened, boolean-merged region of one layer under ``top_cell``.

    ``Region`` over a shape iterator keeps only area-bearing shapes (boxes,
    polygons, paths); texts are ignored.
    """
    if top_cell is None:
        return kdb.Region()
    return kdb.Region(top_cell.begin_shapes_rec(layer_index)).merged()


def _density_windows(
    region: Any, bbox: Any, window_dbu: int, dbu: float, kdb: Any
) -> dict[str, Any]:
    """Tile ``bbox`` with ``window_dbu`` squares and report drawn fraction.

    Windows start at the bbox left/bottom; edge windows are clipped to the
    bbox and judged against their clipped area. Ordered by ``(iy, ix)``.
    """
    windows: list[dict[str, Any]] = []
    iy = 0
    y = bbox.bottom
    while y < bbox.top:
        y2 = min(y + window_dbu, bbox.top)
        ix = 0
        x = bbox.left
        while x < bbox.right:
            x2 = min(x + window_dbu, bbox.right)
            box = kdb.Box(x, y, x2, y2)
            area_dbu2 = (region & kdb.Region(box)).area()
            windows.append(
                {
                    "ix": ix,
                    "iy": iy,
                    "bbox_um": bbox_um_dict(box, dbu),
                    "area_um2": area_dbu2 * dbu * dbu,
                    "fraction": area_dbu2 / (box.width() * box.height()),
                }
            )
            ix += 1
            x = x2
        iy += 1
        y = y2
    fractions = [w["fraction"] for w in windows]
    low = min(windows, key=lambda w: w["fraction"])
    high = max(windows, key=lambda w: w["fraction"])
    return {
        "windows": windows,
        "summary": {
            "min": low["fraction"],
            "max": high["fraction"],
            "mean": sum(fractions) / len(fractions),
            "min_window": {"ix": low["ix"], "iy": low["iy"]},
            "max_window": {"ix": high["ix"], "iy": high["iy"]},
        },
    }


def _density_report(
    layout: Any,
    top_cell: Any,
    bbox: Any,
    window_um: float,
    window_layers: list[tuple[int, int]] | None,
    kdb: Any,
) -> dict[str, Any]:
    dbu = layout.dbu
    window_dbu = round(window_um / dbu)
    if window_um <= 0 or window_dbu <= 0:
        raise StatsError(
            f"--density-window must be a positive size of at least one "
            f"database unit ({dbu} um); got {window_um}"
        )
    if bbox.empty() or bbox.width() == 0 or bbox.height() == 0:
        n_windows = 0
    else:
        n_windows = -(-bbox.width() // window_dbu) * -(-bbox.height() // window_dbu)
    if n_windows > MAX_DENSITY_WINDOWS:
        raise StatsError(
            f"--density-window {window_um} um would tile {n_windows} windows "
            f"per layer (limit {MAX_DENSITY_WINDOWS}); use a larger window"
        )

    layers: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        pair = (info.layer, info.datatype)
        if window_layers is None:
            if is_reserved_annotation_layer(*pair):
                continue
        elif pair not in window_layers:
            continue
        if n_windows == 0:
            continue
        region = _merged_region(top_cell, layer_index, kdb)
        if window_layers is None and region.is_empty():
            continue
        entry: dict[str, Any] = {"layer": pair[0], "datatype": pair[1]}
        entry.update(_density_windows(region, bbox, window_dbu, dbu, kdb))
        layers.append(entry)
    layers.sort(key=lambda e: (e["layer"], e["datatype"]))
    return {"window_um": window_dbu * dbu, "window_count": n_windows, "layers": layers}


def _density(area_dbu2: int, bbox_area_dbu2: int) -> float:
    if bbox_area_dbu2 <= 0:
        return 0.0
    return area_dbu2 / bbox_area_dbu2


def stats_report(
    path: str,
    per_layer: bool = False,
    top: str | None = None,
    density_window: float | None = None,
    window_layers: list[tuple[int, int]] | None = None,
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
                       "vertex_count": .., "flattened_polygon_count": ..,
                       "flattened_vertex_count": ..},
            "layers": None | [
                {"layer": int, "datatype": int, "name": str | None,
                 "area_um2": float, "density": float,
                 "polygon_count": int, "vertex_count": int,
                 "flattened_polygon_count": int,
                 "flattened_vertex_count": int,
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
    defined), matching ``klt layers``' shape-count convention. ``area_um2``
    does not merge overlapping shapes, so it may double-count them.

    ``flattened_polygon_count``/``flattened_vertex_count`` (issue #2366) give
    a consumer that same instance-resolved multiplicity for the polygon and
    vertex counts, without having to flatten the layout itself: a shape
    drawn once in a cell placed N times via a ``CellInstArray`` contributes
    N to these totals, exactly like ``area_um2`` above. They are additive
    fields alongside the existing per-cell-definition ``polygon_count``/
    ``vertex_count`` (which are unchanged), and mirror ``klt layers
    --flattened``'s ``flattened_shapes`` field/convention -- see
    ``docs/cli/layers.md`` -- extended here to also cover vertex counts.

    ``merged_area_um2``/``merged_density`` (issue #3107) are additive
    counterparts of ``area_um2``/``density`` computed on the flattened,
    boolean-merged (unioned) geometry of the reporting scope, so overlapping
    shapes count once. ``total.merged_area_um2`` is the sum of the per-layer
    merged areas (no cross-layer union), keeping ``total == sum(layers)``.

    ``density_window`` (um, issue #3107) adds a ``density_windows`` block of
    per-layer windowed drawn-area fractions over merged geometry (``None``
    when not requested); ``window_layers`` restricts it to ``(layer,
    datatype)`` pairs, default every non-annotation layer with geometry.

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
    total_merged_area_dbu2 = 0
    total_polygon_count = 0
    total_vertex_count = 0
    total_flattened_polygon_count = 0
    total_flattened_vertex_count = 0
    per_layer_entries: list[dict[str, Any]] | None = [] if per_layer else None

    for layer_index in layout.layer_indexes():
        (
            area_dbu2,
            polygon_count,
            vertex_count,
            flattened_polygon_count,
            flattened_vertex_count,
        ) = _accumulate(scope_cells, layer_index, weights)
        merged_area_dbu2 = _merged_region(top_cell, layer_index, kdb).area()
        total_area_dbu2 += area_dbu2
        total_merged_area_dbu2 += merged_area_dbu2
        total_polygon_count += polygon_count
        total_vertex_count += vertex_count
        total_flattened_polygon_count += flattened_polygon_count
        total_flattened_vertex_count += flattened_vertex_count

        if per_layer_entries is not None:
            info = layout.get_info(layer_index)
            per_layer_entries.append(
                {
                    "layer": info.layer,
                    "datatype": info.datatype,
                    "name": info.name if info.name else None,
                    "area_um2": area_dbu2 * dbu * dbu,
                    "density": _density(area_dbu2, bbox_area_dbu2),
                    "merged_area_um2": merged_area_dbu2 * dbu * dbu,
                    "merged_density": _density(merged_area_dbu2, bbox_area_dbu2),
                    "polygon_count": polygon_count,
                    "vertex_count": vertex_count,
                    "flattened_polygon_count": flattened_polygon_count,
                    "flattened_vertex_count": flattened_vertex_count,
                    "annotation": is_reserved_annotation_layer(
                        info.layer, info.datatype
                    ),
                }
            )

    if per_layer_entries is not None:
        per_layer_entries.sort(key=lambda entry: (entry["layer"], entry["datatype"]))

    density_windows = (
        _density_report(layout, top_cell, bbox, density_window, window_layers, kdb)
        if density_window is not None
        else None
    )

    return {
        "schema_version": 1,
        "file": path,
        "dbu_um": dbu,
        "top_cell": top_cell.name if top_cell is not None else None,
        "bbox_um": bbox_um_dict(bbox, dbu),
        "total": {
            "area_um2": total_area_dbu2 * dbu * dbu,
            "density": _density(total_area_dbu2, bbox_area_dbu2),
            "merged_area_um2": total_merged_area_dbu2 * dbu * dbu,
            "merged_density": _density(total_merged_area_dbu2, bbox_area_dbu2),
            "polygon_count": total_polygon_count,
            "vertex_count": total_vertex_count,
            "flattened_polygon_count": total_flattened_polygon_count,
            "flattened_vertex_count": total_flattened_vertex_count,
        },
        "layers": per_layer_entries,
        "density_windows": density_windows,
    }
