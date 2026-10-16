"""``cap_vpp_array`` reference generator PCell for ``klt gen`` (issue #3100).

Arrays one of the PDK's fixed-layout ``cap_vpp_*`` cells. The geometry is
copied from the PDK GDS at produce time (never vendored); request validation,
cell lookup and port reporting live in :mod:`klayout_tools.gen_cap_vpp`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_cap_vpp_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``cap_vpp_array`` PCell -- a row of one PDK fixed cell."""
    import klayout.db as kdb

    # PCellDeclarationHelper exposes its declared params and Type* constants
    # dynamically, which the type checker cannot see; type the base as Any.
    pcell_base: Any = kdb.PCellDeclarationHelper

    class _CapVppArrayPCell(pcell_base):
        """Row of ``num`` instances of one PDK ``cap_vpp_*`` cell."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "pdk_cell",
                self.TypeString,
                "PDK fixed capacitor cell name (sky130_fd_pr__cap_vpp_*)",
                default="sky130_fd_pr__cap_vpp_02p4x04p6_m1m2_noshield",
            )
            self.param(
                "num", self.TypeInt, "Number of cell instances in the row", default=4
            )
            self.param(
                "pitch_um",
                self.TypeDouble,
                "Instance pitch (um); 0 abuts at the cell's bbox width",
                default=0.0,
            )
            self.param(
                "vpp_gds_path",
                self.TypeString,
                "Resolved PDK GDS holding the cell (harness-computed)",
                default="",
            )

        def display_text_impl(self) -> str:
            return f"cap_vpp_array({self.pdk_cell},n={self.num})"

        def produce_impl(self) -> None:
            src = kdb.Layout()
            src.read(self.vpp_gds_path)
            src_cell = src.cell(self.pdk_cell)
            # Importing from the stream keeps the source cell tree intact
            # (cap_vpp cells may reference subcells).
            dst = self.layout.create_cell(self.pdk_cell)
            dst.copy_tree(src_cell)
            bbox = dst.dbbox()
            width = bbox.width()
            pitch = self.pitch_um if self.pitch_um > 0 else width
            for i in range(self.num):
                disp = kdb.DVector(i * pitch - bbox.left, -bbox.bottom)
                self.cell.insert(kdb.DCellInstArray(dst.cell_index(), kdb.DTrans(disp)))

    return {"cap_vpp_array": _CapVppArrayPCell}
