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
from ._layout import load_layout


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


def _accumulate(layout: Any, layer_index: int) -> tuple[int, int, int]:
    """Sum (area_dbu2, polygon_count, vertex_count) for one layer.

    Summed across all cell *definitions* (each shape counted once where it
    is defined, not multiplied by instantiation) — the same convention
    ``klt layers`` uses for shape counts. Overlapping shapes are **not**
    merged, so area may double-count overlapping geometry; this keeps the
    computation cheap and exactly reproducible.
    """
    area_dbu2 = 0
    polygon_count = 0
    vertex_count = 0
    for cell in layout.each_cell():
        for shape in cell.shapes(layer_index).each():
            if not _is_area_shape(shape):
                continue
            area_dbu2 += shape.area()
            polygon_count += 1
            vertex_count += _shape_vertex_count(shape)
    return area_dbu2, polygon_count, vertex_count


def _bbox_um(box: Any, dbu: float) -> dict[str, float]:
    if box.empty():
        return {
            "left": 0.0,
            "bottom": 0.0,
            "right": 0.0,
            "top": 0.0,
            "width": 0.0,
            "height": 0.0,
        }
    return {
        "left": box.left * dbu,
        "bottom": box.bottom * dbu,
        "right": box.right * dbu,
        "top": box.top * dbu,
        "width": box.width() * dbu,
        "height": box.height() * dbu,
    }


def _density(area_dbu2: int, bbox_area_dbu2: int) -> float:
    if bbox_area_dbu2 <= 0:
        return 0.0
    return area_dbu2 / bbox_area_dbu2


def stats_report(path: str, per_layer: bool = False) -> dict[str, Any]:
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

    Area and vertex counts are summed **per cell definition** (each shape
    counted once where it is defined, not multiplied by instantiation),
    matching ``klt layers``' shape-count convention. Overlapping shapes are
    not merged, so area may double-count overlapping geometry.

    ``layers`` is ``None`` unless ``per_layer=True``, in which case it is a
    list sorted by ``(layer, datatype)`` ascending for deterministic output.
    Each entry's ``annotation`` is ``True`` when the layer number falls in
    the reserved annotation-layer range (GDS layers 990-999, any datatype --
    see ``docs/cli/drc.md`` -> "Reserved annotation layer"), matching ``klt
    layers``.

    Raises :class:`StatsError` if the file is missing, unreadable, not a
    recognisable layout stream, or has more than one top cell (ambiguous
    bounding-box reference).
    """
    layout = load_layout(path, StatsError)

    # Imported lazily (after load_layout, which already paid this cost) for
    # kdb.Box() below.
    import klayout.db as kdb

    dbu = layout.dbu

    top_cells = list(layout.top_cells())
    if len(top_cells) > 1:
        names = ", ".join(sorted(c.name for c in top_cells))
        raise StatsError(
            f"layout '{path}' has multiple top cells ({names}); "
            "a single top cell is required for the bounding-box reference"
        )
    top_cell = top_cells[0] if top_cells else None

    bbox = top_cell.bbox() if top_cell is not None else kdb.Box()
    bbox_area_dbu2 = 0 if bbox.empty() else bbox.width() * bbox.height()

    total_area_dbu2 = 0
    total_polygon_count = 0
    total_vertex_count = 0
    per_layer_entries: list[dict[str, Any]] | None = [] if per_layer else None

    for layer_index in layout.layer_indexes():
        area_dbu2, polygon_count, vertex_count = _accumulate(layout, layer_index)
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
        "bbox_um": _bbox_um(bbox, dbu),
        "total": {
            "area_um2": total_area_dbu2 * dbu * dbu,
            "density": _density(total_area_dbu2, bbox_area_dbu2),
            "polygon_count": total_polygon_count,
            "vertex_count": total_vertex_count,
        },
        "layers": per_layer_entries,
    }
