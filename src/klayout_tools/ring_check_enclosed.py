"""Enclosed-polygon classification for ``klt ring-check --ignore-enclosed``.

Moved verbatim out of :mod:`klayout_tools.ring_check` (a pure move, no
behaviour change) so that module stays under its per-file size ceiling.
"""

from __future__ import annotations

from typing import Any


def _partition_enclosed(kdb: Any, polygons: list[Any]) -> tuple[list[Any], list[Any]]:
    """Split ``polygons`` into ``(outer, enclosed)``.

    ``enclosed`` is every polygon that lies strictly inside a hole of another
    polygon in the same list -- the "outermost annulus" classification step
    for ``--ignore-enclosed`` (issue #550). A guard/tap ring drawn around a
    device that shares the ring's own drawn layer merges to two disjoint
    polygons: the annulus, plus whatever sits inside its hole. That second
    polygon is the protected circuit, not a ring fragment, so it belongs in
    ``enclosed`` rather than counting toward the annulus assertion.

    For each polygon that has holes, every *other* polygon whose shape is
    fully contained in one of those holes (checked via ``Region`` boolean
    subtraction: ``other - hole_region`` is empty) is classified as enclosed.
    A polygon that is only partially inside a hole, or lies entirely outside
    every hole (a genuine break in the ring's own perimeter), is never
    reclassified -- so this cannot mask a real fragmentation.
    """
    enclosed_indices: set[int] = set()
    for outer_index, outer in enumerate(polygons):
        hole_count = outer.holes()
        if hole_count == 0:
            continue

        hole_region = kdb.Region()
        for hole_index in range(hole_count):
            hole_points = list(outer.each_point_hole(hole_index))
            hole_region += kdb.Region(kdb.SimplePolygon(hole_points))
        hole_region.merge()

        for other_index, other in enumerate(polygons):
            if other_index == outer_index or other_index in enclosed_indices:
                continue
            if (kdb.Region(other) - hole_region).is_empty():
                enclosed_indices.add(other_index)

    outer_polygons = [p for i, p in enumerate(polygons) if i not in enclosed_indices]
    enclosed_polygons = [p for i, p in enumerate(polygons) if i in enclosed_indices]
    return outer_polygons, enclosed_polygons
