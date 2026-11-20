"""Shared drawing step for composed guard/collector rings (issue #6837).

Four generators -- ``mos_array``, ``diff_pair``, ``esd_device`` and
``bjt_array`` -- draw the same shapes from a composed ``_ring_layout``
result (issue #1493's automatically-sized ring): the tap/collector band, the
metal band over it, its contacts at the ring offset, and -- when the resolved
family needs one -- the implant ring tracking the band. An AST comparison
found those four calls, arguments included, identical in all four generators;
they are factored here once.

Each generator keeps at its own call site only what genuinely differs: the
PFET ring well tie (``mos_array``/``diff_pair``), the collector ring's
tap-role decision (``bjt_array``), and every marker drawing. This module
imports from :mod:`klayout_tools.gen` lazily inside the function, mirroring
the package's import discipline (``gen.py`` imports the factories, never the
reverse).
"""

from __future__ import annotations

from typing import Any


def _draw_composed_guard_ring(
    pcell: Any,
    li_tap: int,
    li_metal: int,
    li_contact: int,
    dbu: float,
    info: dict[str, Any],
) -> None:
    """Draw a composed ring's shared shapes: the tap band, the metal band
    over it, its contacts (all translated by ``info["ring_offset_um"]``,
    which also carries any routing opening cut through both bands), and the
    implant ring tracking the band when the resolved family needs one.

    Shape insertion order per layer is unchanged from the four inlined
    sequences this replaces: tap band, metal band, contacts, implant. A
    caller's own ring shapes drawn on ``well_layer`` (the PFET ring well
    tie) may sit anywhere around this call -- ``well`` never coincides with
    any role layer drawn here, so per-layer order is unaffected (issue
    #6837's verification basis).
    """
    from klayout_tools.gen import (
        _insert_boxes,
        _insert_implant_ring,
        _insert_ring,
        _shift_box,
    )

    ring = info["ring"]
    ox, oy = info["ring_offset_um"]
    gap_box = (
        _shift_box(ring["gap"]["box_um"], ox, oy) if ring["gap"] is not None else None
    )
    outer_box_um = _shift_box(ring["outer_box_um"], ox, oy)
    inner_box_um = _shift_box(ring["inner_box_um"], ox, oy)
    _insert_ring(
        pcell.cell,
        li_tap,
        dbu,
        outer_box_um,
        inner_box_um,
        gap_box,
        pcell.mfg_grid_um,
    )
    _insert_ring(
        pcell.cell,
        li_metal,
        dbu,
        outer_box_um,
        inner_box_um,
        gap_box,
        pcell.mfg_grid_um,
    )
    _insert_boxes(pcell.cell, li_contact, dbu, ring["contact_boxes_um"], ox, oy)
    # The implant tracks the tap/collector band, extended
    # ``ring_implant_margin_um`` past every edge of it (issue #2369's
    # NP.5b/PP.5b extension-beyond-COMP fix to #1580's originally coincident
    # implant) -- still a ring, never a blanket over the enclosed array
    # (mirrors ``well_island``'s own ``well_tap_implant`` ring precedent).
    if pcell.ring_implant_present:
        _insert_implant_ring(
            pcell.cell,
            pcell.layout.layer(pcell.ring_implant_layer),
            dbu,
            outer_box_um,
            inner_box_um,
            gap_box,
            ring["gap"]["side"] if ring["gap"] is not None else None,
            pcell.ring_implant_margin_um,
            pcell.mfg_grid_um,
        )
