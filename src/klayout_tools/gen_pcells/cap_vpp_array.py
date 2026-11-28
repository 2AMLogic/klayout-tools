"""``cap_vpp_array`` reference generator PCell for ``klt gen`` (issue #3100).

Arrays one of the PDK's fixed-layout ``cap_vpp_*`` cells. The geometry is
copied from the PDK GDS at produce time (never vendored); request validation,
cell lookup and port reporting live in :mod:`klayout_tools.gen_cap_vpp`.
:func:`_build_fixed_cell_array_pcell` is the PCell factory ``pdk_cell`` shares.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

from ._common_params import declare_vpp_gds_path


def _build_fixed_cell_array_pcell(
    generator_name: str,
    *,
    pdk_cell_desc: str,
    pdk_cell_default: str,
    num_default: int,
    extra_params: Callable[[Any], None] | None = None,
) -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``generator_name`` PCell -- a row of ``num`` copies of one
    fixed PDK GDS cell (``cap_vpp_array`` and ``pdk_cell`` share it).
    ``extra_params(self)`` declares a generator's own leading parameters; the
    descriptions and defaults passed in are its ``klt gen --list`` contract."""
    import klayout.db as kdb

    # PCellDeclarationHelper exposes its declared params and Type* constants
    # dynamically, which the type checker cannot see; type the base as Any.
    pcell_base: Any = kdb.PCellDeclarationHelper

    class _FixedCellArrayPCell(pcell_base):
        """Row of ``num`` instances of one PDK fixed GDS cell."""

        def __init__(self) -> None:
            super().__init__()
            if extra_params is not None:
                extra_params(self)
            self.param(
                "pdk_cell", self.TypeString, pdk_cell_desc, default=pdk_cell_default
            )
            self.param(
                "num",
                self.TypeInt,
                "Number of cell instances in the row",
                default=num_default,
            )
            self.param(
                "pitch_um",
                self.TypeDouble,
                "Instance pitch (um); 0 abuts at the cell's bbox width",
                default=0.0,
            )
            declare_vpp_gds_path(self)

        def display_text_impl(self) -> str:
            return f"{generator_name}({self.pdk_cell},n={self.num})"

        def produce_impl(self) -> None:
            src = kdb.Layout()
            src.read(self.vpp_gds_path)
            src_cell = src.cell(self.pdk_cell)
            # Importing from the stream keeps the source cell tree intact
            # (the fixed cell may reference subcells).
            dst = self.layout.create_cell(self.pdk_cell)
            dst.copy_tree(src_cell)
            bbox = dst.dbbox()
            width = bbox.width()
            pitch = self.pitch_um if self.pitch_um > 0 else width
            for i in range(self.num):
                disp = kdb.DVector(i * pitch - bbox.left, -bbox.bottom)
                self.cell.insert(kdb.DCellInstArray(dst.cell_index(), kdb.DTrans(disp)))

    return {generator_name: _FixedCellArrayPCell}


def _build_cap_vpp_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``cap_vpp_array`` PCell -- a row of one PDK fixed cell."""
    return _build_fixed_cell_array_pcell(
        "cap_vpp_array",
        pdk_cell_desc="PDK fixed capacitor cell name (sky130_fd_pr__cap_vpp_*)",
        pdk_cell_default="sky130_fd_pr__cap_vpp_02p4x04p6_m1m2_noshield",
        num_default=4,
    )
