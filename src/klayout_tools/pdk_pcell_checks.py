"""Post-instantiation validity check for a vendor PCell variant.

Split out of :mod:`klayout_tools.pdk_pcell` (file-size ratchet). The helper
imports ``PdkPCellError`` lazily because that module imports this one at
call time, not import time.
"""

from __future__ import annotations

from typing import Any


def check_pcell_variant(
    layout: Any, variant: int, library: str, pcell_name: str
) -> None:
    """Raise :class:`PdkPCellError` unless ``variant`` names a usable cell.

    KLayout does not propagate an exception a vendor PCell raises inside
    ``produce_impl``: ``PCellDeclarationHelper.produce`` prints ``ERROR:
    ...`` to stderr and ``add_pcell_variant`` still returns -- measured on
    gf180mcuA's ``cap_mim`` with ``GF_PDK_OPTION`` unset (issue #5936) as a
    *valid* index whose cell has an empty name and no geometry. An unnamed
    cell is what makes the written GDS unreadable ("Empty cell name" on
    read-back), so an empty name is the failure signal -- not geometry
    count, which a vendor PCell may legitimately drive to zero.
    """
    from .pdk_pcell import PdkPCellError

    cell = layout.cell(variant) if layout.is_valid_cell_index(variant) else None
    if cell is None or not cell.name:
        raise PdkPCellError(
            f"PDK PCell '{library}/{pcell_name}' failed while producing "
            "geometry: KLayout caught the vendor PCell's exception (see the "
            "'ERROR:' line on stderr) and left the variant cell unnamed -- "
            "check the PCell's requirements, e.g. required environment "
            "variables or parameter values"
        )
