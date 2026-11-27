"""Draw the shared tap, metal, contact and implant geometry of ring PCells.

The standalone guard ring and well island draw at the origin. Composed MOS,
differential pair, ESD and BJT rings use the offset computed by the generator.
Each caller selects its tap and implant layers and draws its own well ties,
voltage markers and labels. The drawing functions are imported lazily because
:mod:`klayout_tools.gen` imports these PCell factories.
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
    """Draw the translated ring supplied by a composed generator."""
    _draw_ring_shapes(
        pcell,
        li_tap,
        li_metal,
        li_contact,
        dbu,
        info["ring"],
        offset_um=info["ring_offset_um"],
        implant_layer=pcell.ring_implant_layer if pcell.ring_implant_present else None,
    )


def _draw_ring_shapes(
    pcell: Any,
    li_tap: int,
    li_metal: int,
    li_contact: int,
    dbu: float,
    ring: dict[str, Any],
    *,
    implant_layer: Any | None,
    offset_um: tuple[float, float] = (0.0, 0.0),
) -> None:
    """Draw the tap, metal, contacts and implant in per-layer insertion order.

    The well occupies a separate layer from these four roles. The caller can
    therefore draw its well before or after this step (#6837).
    """
    from klayout_tools.gen import (
        _insert_boxes,
        _insert_implant_ring,
        _insert_ring,
        _shift_box,
    )

    ox, oy = offset_um
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
    # NP.5b/PP.5b require the implant to extend past each edge of the tap
    # band (#2369, following #1580). Keep the enclosed devices and the
    # routing opening clear when applying that margin.
    if implant_layer is not None:
        _insert_implant_ring(
            pcell.cell,
            pcell.layout.layer(implant_layer),
            dbu,
            outer_box_um,
            inner_box_um,
            gap_box,
            ring["gap"]["side"] if ring["gap"] is not None else None,
            pcell.ring_implant_margin_um,
            pcell.mfg_grid_um,
        )
