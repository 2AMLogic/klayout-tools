"""Y-band partition of a leaf cell for ``klt economy --attribute`` (issue #5189).

Split out of ``economy.py`` for the file-size and complexity ratchets.
"""

from typing import Any

from ._layout import bbox_um_dict


def _layer_regions(
    kdb: Any, layout: Any, top_cell: Any, annotation_pairs: set[tuple[int, int]]
) -> dict[str, Any]:
    """Merged drawn region per non-annotation layer, keyed ``layer/datatype``."""
    regions: dict[str, Any] = {}
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        if (info.layer, info.datatype) in annotation_pairs:
            continue
        region = kdb.Region(top_cell.shapes(li)).merged()
        if not region.is_empty():
            regions[f"{info.layer}/{info.datatype}"] = region
    return regions


def _strip_edges(regions: dict[str, Any], bbox: Any) -> list[int]:
    """Sorted Y coordinates of every merged-polygon vertex, plus the bbox
    bottom/top, clipped to the bbox."""
    edges = {bbox.bottom, bbox.top}
    for region in regions.values():
        for polygon in region.each():
            edges.update(pt.y for pt in polygon.each_point_hull())
            for h in range(polygon.holes()):
                edges.update(pt.y for pt in polygon.each_point_hole(h))
    return sorted(y for y in edges if bbox.bottom <= y <= bbox.top)


def _strip_layers(
    kdb: Any, regions: dict[str, Any], strip: Any
) -> tuple[list[str], int]:
    """Layer names overlapping ``strip`` and the strip's drawn dbu^2."""
    names = [n for n, r in sorted(regions.items()) if not (r & strip).is_empty()]
    union = kdb.Region()
    for name in names:
        union += regions[name]
    return names, (union.merged() & strip).area()


def _merge_strips(
    strips: list[tuple[int, int, list[str], int]],
) -> list[list[Any]]:
    """Merge neighbouring strips with equal layer sets: ``[y0, y1, names, drawn]``."""
    merged: list[list[Any]] = []
    for y0, y1, names, drawn in strips:
        if merged and merged[-1][2] == names:
            merged[-1][1] = y1
            merged[-1][3] += drawn
        else:
            merged.append([y0, y1, names, drawn])
    return merged


def leaf_bands(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    bbox: Any,
    dbu: float,
    annotation_pairs: set[tuple[int, int]],
) -> list[dict[str, Any]]:
    """Y-axis partition of a leaf cell's bbox into full-width bands keyed by
    the set of drawn layers overlapping each band.

    Elementary strips between consecutive merged-polygon vertex Y edges are
    merged when their layer sets are equal, so band areas sum exactly to the
    bbox area (integer dbu^2). Annotation layers are excluded.
    """
    if bbox.empty():
        return []
    regions = _layer_regions(kdb, layout, top_cell, annotation_pairs)
    ys = _strip_edges(regions, bbox)
    strips = []
    for y0, y1 in zip(ys, ys[1:], strict=False):
        strip = kdb.Region(kdb.Box(bbox.left, y0, bbox.right, y1))
        names, drawn = _strip_layers(kdb, regions, strip)
        strips.append((y0, y1, names, drawn))
    bands = []
    for y0, y1, names, drawn in _merge_strips(strips):
        box = kdb.Box(bbox.left, y0, bbox.right, y1)
        bands.append(
            {
                "bbox_um": bbox_um_dict(box, dbu),
                "area_um2": box.area() * dbu * dbu,
                "layers": names,
                "drawn_area_um2": drawn * dbu * dbu,
            }
        )
    return bands
