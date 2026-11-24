"""Per-gap drawn coverage for ``klt economy --attribute`` (issue #6941).

The attribution block's ``gap_components`` partition says *where* the
non-child area sits, but a hand-placed analog top cell draws real structure
inside those gaps -- guard rings, reserved-but-empty regions, top-level
strap corridors -- and a single ``area_um2`` figure cannot tell a ring from
dead whitespace. This module decomposes each gap polygon's contents into
connected components of top-cell-only drawn geometry, so rings and
corridors surface as bbox/area/piece-count clusters instead of collapsing
into one margin blob.

Sourcing matches ``top_level_drawn_area_um2`` exactly: the non-recursive,
annotation-filtered shapes of the selected top cell (with
``--flatten-wrappers``, drawing still comes from the top cell only -- a
wrapper's own drawing between its leaves is inside the wrapper, not the
top). The coverage is intersected with the gap polygon *before*
connectivity, so a child footprint splits a structure that crosses it into
separate components. All geometry stays in integer database units via
``klayout.db`` region booleans (headless, CI-runnable).
"""

from __future__ import annotations

from typing import Any

from ._layout import bbox_um_dict
from .economy_leaves import gap_position


def top_level_pieces(
    layout: Any,
    top_cell: Any,
    annotation_pairs: set[tuple[int, int]],
) -> list[Any]:
    """Polygons of every eligible shape drawn directly in ``top_cell``
    (non-recursive, annotation-filtered), in deterministic layer then shape
    order.

    These are the *source* shapes for ``piece_count``: the merged coverage
    region alone cannot say how many original shapes compose a component,
    and a shape a child bbox splits contributes to each resulting
    component, so the originals must be retained individually. Texts carry
    no area and are skipped.
    """
    pieces: list[Any] = []
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        if (info.layer, info.datatype) in annotation_pairs:
            continue
        for shape in top_cell.shapes(li).each():
            if shape.is_text():
                continue
            pieces.append(shape.polygon)
    return pieces


def gap_drawn_coverage(
    kdb: Any,
    gap_polygon: Any,
    top_local: Any,
    pieces: list[Any],
    dbu: float,
) -> tuple[int, list[dict[str, Any]]]:
    """Decompose one gap polygon's drawn coverage.

    ``top_local`` is the merged non-recursive top-cell region (the same
    geometry ``top_level_drawn_area_um2`` totals -- layers already merged,
    so overlaps count once); ``pieces`` are :func:`top_level_pieces`
    source polygons. The coverage is the intersection with the actual gap
    polygon -- its holes respected -- taken before connectivity so a child
    footprint splits a crossing structure.

    Returns ``(drawn_area_dbu2, components)``; each component is
    ``{bbox_um, drawn_area_um2, piece_count}``, sorted by drawn area
    descending then bbox position (deterministic for equal-area
    components). ``piece_count`` counts source shapes with positive-area
    overlap with the component, each once per component.
    """
    coverage = (top_local & kdb.Region(gap_polygon)).merged()
    components: list[dict[str, Any]] = []
    total = 0
    for polygon in coverage.each():
        area = polygon.area()
        if area <= 0:
            continue
        total += area
        region = kdb.Region(polygon)
        pb = polygon.bbox()
        count = 0
        for piece in pieces:
            if (piece.bbox() & pb).empty():
                continue
            if (kdb.Region(piece) & region).area() > 0:
                count += 1
        components.append(
            {
                "bbox_um": bbox_um_dict(pb, dbu),
                "drawn_area_um2": area * dbu * dbu,
                "piece_count": count,
            }
        )
    components.sort(
        key=lambda c: (
            -c["drawn_area_um2"],
            c["bbox_um"]["left"],
            c["bbox_um"]["bottom"],
            c["bbox_um"]["right"],
            c["bbox_um"]["top"],
        )
    )
    return total, components


def gap_rows(
    kdb: Any,
    remainder: Any,
    bbox: Any,
    placed: list[tuple[str, Any]],
    top_local: Any,
    pieces: list[Any],
    dbu: float,
) -> tuple[int, list[dict[str, Any]]]:
    """Gap rows (position, bbox, area, drawn coverage) plus their dbu^2
    total, for ``_attribution_block`` (issue #4660 rows, #6941 coverage).

    ``remainder`` is the merged bbox-minus-child-bboxes region;
    ``top_local``/``pieces`` are the top cell's merged local region and
    :func:`top_level_pieces` source polygons. Rows are sorted by area
    descending, then position label, then bbox left -- the #4660 order.
    """
    gaps: list[dict[str, Any]] = []
    gap_total = 0
    for polygon in remainder.each():
        area = polygon.area()
        if area <= 0:
            continue
        gap_total += area
        gb = polygon.bbox()
        drawn_dbu2, drawn_components = gap_drawn_coverage(
            kdb, polygon, top_local, pieces, dbu
        )
        gaps.append(
            {
                "position": gap_position(gb, bbox, placed),
                "bbox_um": bbox_um_dict(gb, dbu),
                "area_um2": area * dbu * dbu,
                "drawn_area_um2": drawn_dbu2 * dbu * dbu,
                "drawn_components": drawn_components,
            }
        )
    gaps.sort(key=lambda g: (-g["area_um2"], g["position"], g["bbox_um"]["left"]))
    return gap_total, gaps
