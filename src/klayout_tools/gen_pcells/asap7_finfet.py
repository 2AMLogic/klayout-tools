"""``asap7_finfet`` reference generator PCell for ``klt gen`` (issue #2762).

Geometry comes from :func:`klayout_tools.gen_asap7.finfet_layout`, shared with
the describe/validate callbacks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_asap7_finfet_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``asap7_finfet`` PCell (lazy ``klayout.db`` import, like the
    sibling ``_build_<family>_pcell`` factories)."""
    import klayout.db as kdb

    from klayout_tools.gen_asap7 import (
        ASAP7_DBU_UM,
        ASAP7_LAYERS,
        finfet_layout,
    )

    class _Asap7FinFETPCell(kdb.PCellDeclarationHelper):
        """Multi-fin, multi-finger ASAP7 nFET/pFET on the fin and gate grids."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "device",
                self.TypeString,  # type: ignore[attr-defined]
                "'nfet' (default) or 'pfet'",
                default="nfet",
            )
            self.param(
                "nfin",
                self.TypeInt,  # type: ignore[attr-defined]
                "Fins per gate finger (1..16)",
                default=2,
            )
            self.param(
                "nf",
                self.TypeInt,  # type: ignore[attr-defined]
                "Gate fingers (1..16); extracted nfin is nfin * nf",
                default=1,
            )
            self.param(
                "vt",
                self.TypeString,  # type: ignore[attr-defined]
                "Threshold flavour: 'rvt' (default), 'lvt' or 'slvt'",
                default="rvt",
            )

        def display_text_impl(self) -> str:
            return f"asap7_finfet({self.device},{self.nfin}x{self.nf},{self.vt})"  # type: ignore[attr-defined]

        def produce_impl(self) -> None:
            info = finfet_layout(
                self.device,  # type: ignore[attr-defined]
                self.nfin,  # type: ignore[attr-defined]
                self.nf,  # type: ignore[attr-defined]
                self.vt,  # type: ignore[attr-defined]
            )
            # The PCell is produced in the library layout (dbu 0.001), not the
            # 0.00025 output layout, so convert the integer-dbu geometry to
            # this layout's dbu; KLayout rescales on instantiation.
            scale = ASAP7_DBU_UM / self.layout.dbu
            for role, boxes in info["boxes"].items():
                li = self.layout.layer(kdb.LayerInfo(*ASAP7_LAYERS[role]))
                for b in boxes:
                    self.cell.shapes(li).insert(
                        kdb.Box(*(int(round(v * scale)) for v in b))
                    )

    return {"asap7_finfet": _Asap7FinFETPCell}
