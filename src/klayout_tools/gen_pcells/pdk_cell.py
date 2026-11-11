"""``pdk_cell`` reference generator PCell for ``klt gen`` (issue #5290).

Arrays one of a PDK's fixed, pre-drawn GDS cells. The geometry is copied from
the PDK GDS at produce time (never vendored); request validation, cell lookup
and port reporting live in :mod:`klayout_tools.gen_pdk_cell`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_pdk_cell_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``pdk_cell`` PCell -- a row of one PDK fixed cell."""
    import klayout.db as kdb

    pcell_base: Any = kdb.PCellDeclarationHelper

    class _PdkCellPCell(pcell_base):
        """Row of ``num`` instances of one PDK fixed GDS cell."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "library",
                self.TypeString,
                "PDK library directory under libs.ref (e.g. sg13g2_io)",
                default="",
            )
            self.param("pdk_cell", self.TypeString, "PDK fixed cell name", default="")
            self.param(
                "num", self.TypeInt, "Number of cell instances in the row", default=1
            )
            self.param(
                "pitch_um",
                self.TypeDouble,
                "Instance pitch (um); 0 abuts at the cell's bbox width",
                default=0.0,
            )
            self.param(
                "pdk_cell_gds_path",
                self.TypeString,
                "Resolved PDK GDS holding the cell (harness-computed)",
                default="",
            )

        def display_text_impl(self) -> str:
            return f"pdk_cell({self.pdk_cell},n={self.num})"

        def produce_impl(self) -> None:
            src = kdb.Layout()
            src.read(self.pdk_cell_gds_path)
            src_cell = src.cell(self.pdk_cell)
            dst = self.layout.create_cell(self.pdk_cell)
            dst.copy_tree(src_cell)
            bbox = dst.dbbox()
            pitch = self.pitch_um if self.pitch_um > 0 else bbox.width()
            for i in range(self.num):
                disp = kdb.DVector(i * pitch - bbox.left, -bbox.bottom)
                self.cell.insert(kdb.DCellInstArray(dst.cell_index(), kdb.DTrans(disp)))

    return {"pdk_cell": _PdkCellPCell}
