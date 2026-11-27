"""Operands of the ordinary licon.1 and ct.1_a/b size checks in sky130A_mr.drc."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._layout import region

if TYPE_CHECKING:
    import klayout.db as kdb

RPM, URPM = (86, 20), (79, 20)
PSDM, POLY, POLY_RS = (94, 20), (66, 20), (66, 13)
AREAID_CE = (81, 2)

ORDINARY_LICON = "sky130_ordinary_licon"
ORDINARY_MCON = "sky130_ordinary_mcon"

CUT_SELECTION_LAYERS = {
    ORDINARY_LICON: (RPM, URPM, PSDM, POLY, POLY_RS),
    ORDINARY_MCON: (AREAID_CE,),
}


def ordinary_licon(cell: kdb.Cell, cuts: kdb.Region) -> kdb.Region:
    """Subtract the precision-resistor region, retaining unmasked cut fragments."""
    layout = cell.layout()
    markers = region(layout, cell, RPM) | region(layout, cell, URPM)
    if markers.is_empty():
        return cuts
    resistor_poly = region(layout, cell, POLY).interacting(
        region(layout, cell, POLY_RS)
    )
    return cuts - (markers & region(layout, cell, PSDM) & resistor_poly)


def ordinary_mcon(cell: kdb.Cell, cuts: kdb.Region) -> kdb.Region:
    """Select entire non-ring contacts outside areaid:ce, including edge touches."""
    non_ring = cuts.merged().with_holes(0, 1, False)
    return non_ring.outside(region(cell.layout(), cell, AREAID_CE))
