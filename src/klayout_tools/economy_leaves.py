"""Leaf-placement collection for ``klt economy --attribute`` (issue #5179).

Split out of ``economy.py`` for the file-size ratchet (gap-position labels
moved here alongside, issue #5189).
"""

from collections.abc import Callable
from typing import Any


def collect_leaf_placements(
    kdb: Any,
    top_cell: Any,
    drawn_area: Callable[[Any], float],
    flatten_wrappers: bool,
) -> list[tuple[str, str, Any, float]]:
    """Leaf placements ``(name, cell, bbox in top coords, drawn dbu^2)`` of
    the top cell's direct instances; wrapper children (two or more direct
    instances) are recursively expanded when ``flatten_wrappers``, named by
    hierarchical path (``COMP[0]/OTA[0]``). ``drawn_area(cell)`` returns the
    cell's merged drawn area in dbu^2 (callers cache it)."""

    def collect(
        cell: Any, parent_trans: Any, prefix: str
    ) -> list[tuple[str, str, Any, float]]:
        elems: list[tuple[Any, Any, Any]] = []
        for inst in cell.each_inst():
            child = inst.cell
            child_bbox = child.bbox()
            if child_bbox.empty():
                continue
            for trans in inst.cell_inst.each_cplx_trans():
                composed = parent_trans * trans
                elems.append((child, composed, child_bbox.transformed(composed)))
        # Stable names: ordinal among same-cell instances by position.
        elems.sort(
            key=lambda e: (e[0].name, e[2].left, e[2].bottom, e[2].right, e[2].top)
        )
        counts: dict[str, int] = {}
        out: list[tuple[str, str, Any, float]] = []
        for child, composed, ibox in elems:
            idx = counts.get(child.name, 0)
            counts[child.name] = idx + 1
            name = f"{prefix}{child.name}[{idx}]"
            if flatten_wrappers and child.child_instances() >= 2:
                out.extend(collect(child, composed, name + "/"))
            else:
                out.append(
                    (name, child.name, ibox, drawn_area(child) * composed.mag**2)
                )
        return out

    return collect(top_cell, kdb.ICplxTrans(), "")


def _margin_edges(gap_bbox: Any, bbox: Any) -> list[str]:
    """Top-bbox edges the gap component touches, in fixed order."""
    checks = (
        ("bottom", gap_bbox.bottom <= bbox.bottom),
        ("left", gap_bbox.left <= bbox.left),
        ("right", gap_bbox.right >= bbox.right),
        ("top", gap_bbox.top >= bbox.top),
    )
    return [edge for edge, hit in checks if hit]


def _single_neighbour_position(gap_bbox: Any, name: str, ibox: Any) -> str:
    if gap_bbox.left >= ibox.right:
        return f"right_of:{name}"
    if gap_bbox.right <= ibox.left:
        return f"left_of:{name}"
    if gap_bbox.bottom >= ibox.top:
        return f"above:{name}"
    return f"below:{name}"


def gap_position(gap_bbox: Any, bbox: Any, placed: list[tuple[str, Any]]) -> str:
    """Deterministic position label for one gap component.

    ``margin:<edges>`` when the component touches the top bbox boundary
    (edges joined by ``+`` in a fixed order); otherwise ``between:<a>,<b>``
    when it abuts two or more instance bboxes (names sorted), or
    ``left_of|right_of|above|below:<inst>`` when it abuts exactly one.
    """
    edges = _margin_edges(gap_bbox, bbox)
    if edges:
        return "margin:" + "+".join(edges)

    neighbours = sorted(
        (name, ibox)
        for name, ibox in placed
        if ibox.left <= gap_bbox.right
        and ibox.right >= gap_bbox.left
        and ibox.bottom <= gap_bbox.top
        and ibox.top >= gap_bbox.bottom
    )
    if len(neighbours) >= 2:
        return "between:" + ",".join(n for n, _ in neighbours)
    if neighbours:
        return _single_neighbour_position(gap_bbox, *neighbours[0])
    return "interior"
