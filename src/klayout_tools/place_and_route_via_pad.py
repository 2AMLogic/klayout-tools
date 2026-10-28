"""ASAP7 post-route ``VIA_*`` M1 landing-pad trim for the DEF -> GDS merge
(issue #4495).

OpenROAD's ``VIA_*`` cells carry an M1 landing pad sized for TritonRoute's
own spacing rules, which the packaged ASAP7 deck's M1 tip rules
(``M1.S.2``-``M1.S.5``) reject; ``route_drc_violation_count`` stays 0 because
the router's rule set does not carry them. This pass drops a pad polygon only
when every placed instance of its via cell has that polygon fully covered by
routed/cell M1 *outside* any ``VIA_*`` cell, so connectivity is preserved by
construction (overlap is same-net or already a short). It is a documented
no-op for every other PDK family -- the GDS is not touched at all.
"""

from __future__ import annotations

from typing import Any

from .pdk_families import pdk_variant_family

_ASAP7_FAMILY = "asap7"
_M1_LEF_LAYER = "M1"
_VIA_PREFIX = "VIA_"


def _report(status: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "cells_touched": 0,
        "pads_trimmed": 0,
        "pads_left": 0,
    }


def _wire_region(kdb: Any, top_cell: Any, m1_index: int, via_indices: set[int]) -> Any:
    """M1 wire = every M1 shape reachable from the top cell except through a
    ``VIA_*`` cell (router wires, PDN, standard-cell pin/obstruction metal)."""
    it = top_cell.begin_shapes_rec(m1_index)
    it.unselect_cells(sorted(via_indices))
    return kdb.Region(it).merged()


def _top_level_placements(top_cell: Any, via: Any) -> list[Any]:
    """Placements of ``via`` in the top cell; empty when it is also nested in
    another cell (conservative: the caller then leaves its pads alone)."""
    if any(p != top_cell.cell_index() for p in via.each_parent_cell()):
        return []
    placements: list[Any] = []
    for inst in top_cell.each_inst():
        if inst.cell_index == via.cell_index():
            placements.extend(inst.cell_inst.each_cplx_trans())
    return placements


def _split_pads(
    kdb: Any, pads: Any, placements: list[Any], wire: Any
) -> tuple[Any, int]:
    """Return ``(kept pads, trimmed count)``: a pad is dropped only when wire
    fully covers it in every placement."""
    keep = kdb.Region()
    trimmed = 0
    for pad in pads.each():
        single = kdb.Region(pad)
        if all((single.transformed(t) - wire).is_empty() for t in placements):
            trimmed += 1
        else:
            keep.insert(pad)
    return keep, trimmed


def trim_via_pads(
    kdb: Any,
    *,
    layout: Any,
    top_cell: Any,
    variant: str,
    lef_to_gds: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """Trim ASAP7 ``VIA_*`` M1 pads that are redundant with routed M1 wire.

    Returns ``status`` (``"trimmed"``: at least one pad dropped; ``"clean"``:
    ran, nothing to drop; ``"skipped"``: could not run, ``reason`` says why --
    non-ASAP7 PDKs report ``"skipped"`` and leave the layout untouched),
    ``reason``, ``cells_touched``, ``pads_trimmed`` and ``pads_left`` (pad
    polygons not fully covered by wire in every instance, left in place).
    Never raises for absent input.
    """
    if pdk_variant_family(variant) != _ASAP7_FAMILY:
        return _report("skipped", f"PDK family of '{variant}' is not ASAP7; no-op")
    m1 = lef_to_gds.get(_M1_LEF_LAYER)
    if m1 is None:
        return _report("skipped", "no M1 routed-net layer in the KLayout layer map")
    m1_index = layout.find_layer(*m1)
    if m1_index is None:
        return _report("clean")

    via_cells = [
        c
        for c in layout.each_cell()
        if c.name.startswith(_VIA_PREFIX) and c.cell_index() != top_cell.cell_index()
    ]
    if not via_cells:
        return _report("clean")
    wire = _wire_region(kdb, top_cell, m1_index, {c.cell_index() for c in via_cells})

    report = _report("clean")
    for via in via_cells:
        pads = kdb.Region(via.shapes(m1_index)).merged()
        if pads.is_empty():
            continue
        placements = _top_level_placements(top_cell, via)
        if not placements:
            report["pads_left"] += pads.count()
            continue
        keep, trimmed = _split_pads(kdb, pads, placements, wire)
        report["pads_trimmed"] += trimmed
        report["pads_left"] += pads.count() - trimmed
        if trimmed:
            via.shapes(m1_index).clear()
            via.shapes(m1_index).insert(keep)
            report["cells_touched"] += 1
    if report["pads_trimmed"]:
        report["status"] = "trimmed"
    return report
