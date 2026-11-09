"""Leaf-placement collection for ``klt economy --attribute`` (issue #5179).

Split out of ``economy.py`` for the file-size ratchet.
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
