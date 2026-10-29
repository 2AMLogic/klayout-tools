"""``cap_array`` ``stack: 2`` geometry (issue #3332): sky130's two MiM levels
over one footprint, wired as a single two-terminal capacitor.

The stack is met3 / ``capm`` / met4 / ``capm2`` / met5. met4 is the shared
*middle* plate: it is both the top plate's landing metal of the ``capm`` level
(``via3``) and the bottom plate of the ``capm2`` level. "Parallel" therefore
means tying the two outer plates (met3 and met5) together, with met4 as the
other terminal. The tie is made *outside* the capacitor footprint -- a met3
tab, ``via3``, a met4 pad, ``via4`` and a met5 pad stacked south of the bank
-- so the extraction-side top-plate-via exclusion (which only suppresses the
``via3``/``via4`` cuts that land on a ``capm``/``capm2`` plate) never sees it.

Builds on :func:`klayout_tools.gen._cap_parallel_layout`'s shared bottom
plate, per-unit top plates and ``via3`` grids; this module only adds the
second level and the outer-plate strap.
"""

from __future__ import annotations

from typing import Any

from .gen_layer_tables import (
    CAP_PARALLEL_VIA_EDGE_INSET_UM,
    CAP_TOP_ESCAPE_CLEARANCE_UM,
)

Box = tuple[float, float, float, float]


def _cap_stack_layout(
    plate_w_um: float,
    plate_h_um: float,
    unit: dict[str, Any],
    cells: list[dict[str, Any]],
    par: dict[str, Any],
    top_via_min_w_um: float,
    geom: dict[str, float],
) -> dict[str, Any]:
    """Boxes (absolute ``cap_array`` coordinates, um) of a ``stack: 2`` bank.

    ``par`` is the :func:`~klayout_tools.gen._cap_parallel_layout` result for
    the same bank; ``geom`` is the family's
    :data:`~klayout_tools.gen_layer_tables._PDK_CAP_STACK_GEOMETRY_UM` entry."""
    from .gen import _GRID_DBU_UM, CONTACT_SIZE_UM, _snap_square_box_um

    via4 = geom["cap2_via_side_um"]
    enc = geom["cap2_metal_via_enclosure_um"]
    pad_side = max(geom["cap2_metal_min_w_um"], via4 + 2 * enc)
    pad_half = pad_side / 2.0
    bank_w = par["total_w_um"]
    bottom_h = par["total_h_um"]
    cxb = bank_w / 2.0
    cy = bottom_h / 2.0

    # Outer-plate strap: stacked pads south of the bank, clear of the met4
    # middle plate (which spans y = 0..bottom_h).
    pcy = -(CAP_TOP_ESCAPE_CLEARANCE_UM + pad_half)
    pad = _snap_square_box_um(cxb, pcy, pad_half, _GRID_DBU_UM)
    via3_pad = _snap_square_box_um(
        cxb, pcy, max(CONTACT_SIZE_UM, top_via_min_w_um) / 2.0, _GRID_DBU_UM
    )
    via4_pad = _snap_square_box_um(cxb, pcy, via4 / 2.0, _GRID_DBU_UM)
    met3_tab: Box = (pad[0], pad[1], pad[2], 0.0)

    # via4 grid over every capm2 (same footprint as capm).
    pitch = 2.0 * via4
    room_x = plate_w_um - 2 * CAP_PARALLEL_VIA_EDGE_INSET_UM - via4
    room_y = plate_h_um - 2 * CAP_PARALLEL_VIA_EDGE_INSET_UM - via4
    nx = max(1, int(room_x / pitch + 1e-9) + 1)
    ny = max(1, int(room_y / pitch + 1e-9) + 1)
    vias4: list[Box] = []
    for c in cells:
        ucx = c["x0_um"] + unit["total_w_um"] / 2.0
        for ix in range(nx):
            for iy in range(ny):
                vx = ucx + (ix - (nx - 1) / 2.0) * pitch
                vy = cy + (iy - (ny - 1) / 2.0) * pitch
                vias4.append(_snap_square_box_um(vx, vy, via4 / 2.0, _GRID_DBU_UM))
    first_cx = cells[0]["x0_um"] + unit["total_w_um"] / 2.0
    last_cx = cells[-1]["x0_um"] + unit["total_w_um"] / 2.0
    grid_half_w = max(pad_half, (nx - 1) * pitch / 2.0 + via4 / 2.0 + enc)
    grid_half_h = max(pad_half, (ny - 1) * pitch / 2.0 + via4 / 2.0 + enc)
    strap: Box = (
        first_cx - grid_half_w,
        cy - grid_half_h,
        last_cx + grid_half_w,
        cy + grid_half_h,
    )
    met5_stub: Box = (pad[0], pad[1], pad[2], cy)

    par_boxes = par["boxes_um"]
    # par's top_via_metal is [strap, stub, escape pad]; the middle plate
    # replaces the strap, the north stub + pad stay as the middle terminal.
    middle_plate: Box = (0.0, 0.0, bank_w, bottom_h)
    return {
        "boxes_um": {
            "bottom_plate": [*par_boxes["bottom_plate"], met3_tab],
            "top_plate": par_boxes["top_plate"],
            "top_via": [*par_boxes["top_via"], via3_pad],
            "top_via_metal": [middle_plate, *par_boxes["top_via_metal"][1:], pad],
            "cap2_top_plate": par_boxes["top_plate"],
            "cap2_top_via": [*vias4, via4_pad],
            "cap2_top_via_metal": [strap, met5_stub, pad],
        },
        "total_w_um": bank_w,
        "total_h_um": bottom_h,
        "vias_per_unit": par["vias_per_unit"],
        "cap2_vias_per_unit": nx * ny,
        "outer_xy": (cxb, pcy),
        "outer_pad_w_um": pad_side,
        "mid_xy": par["top_escape_xy"],
        "mid_pad_w_um": par["top_pad_w_um"],
    }
