"""``asap7_tap`` substrate/well tap PCell for ``klt gen`` (issue #4841).

Geometry comes from :func:`klayout_tools.gen_asap7_tap.tap_layout`, shared
with the describe/validate callbacks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_asap7_tap_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``asap7_tap`` PCell (lazy ``klayout.db`` import)."""
    import klayout.db as kdb

    from klayout_tools.gen_asap7 import ASAP7_DBU_UM, ASAP7_LAYERS
    from klayout_tools.gen_asap7_tap import tap_layout

    class _Asap7TapPCell(kdb.PCellDeclarationHelper):
        """ASAP7 substrate (ptap) or n-well (ntap) tap, optionally a ring."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "device",
                self.TypeString,  # type: ignore[attr-defined]
                "'ptap' (default, p+ in substrate) or 'ntap' (n+ in n-well)",
                default="ptap",
            )
            self.param(
                "nfin",
                self.TypeInt,  # type: ignore[attr-defined]
                "Tap height in fins (2..16)",
                default=2,
            )
            self.param(
                "ncol",
                self.TypeInt,  # type: ignore[attr-defined]
                "S/D contact columns (1..16)",
                default=1,
            )
            self.param(
                "ring",
                self.TypeBoolean,  # type: ignore[attr-defined]
                "Draw a rectangular tap ring (needs ncol >= 2)",
                default=False,
            )
            self.param(
                "inner_nfin",
                self.TypeInt,  # type: ignore[attr-defined]
                "Ring only: fins of hole between the top and bottom bars",
                default=2,
            )

        def display_text_impl(self) -> str:
            return (
                f"asap7_tap({self.device},{self.nfin}x{self.ncol}"  # type: ignore[attr-defined]
                f"{',ring' if self.ring else ''})"  # type: ignore[attr-defined]
            )

        def produce_impl(self) -> None:
            info = tap_layout(
                self.device,  # type: ignore[attr-defined]
                self.nfin,  # type: ignore[attr-defined]
                self.ncol,  # type: ignore[attr-defined]
                bool(self.ring),  # type: ignore[attr-defined]
                self.inner_nfin,  # type: ignore[attr-defined]
            )
            scale = ASAP7_DBU_UM / self.layout.dbu
            for role, boxes in info["boxes"].items():
                li = self.layout.layer(kdb.LayerInfo(*ASAP7_LAYERS[role]))
                for b in boxes:
                    self.cell.shapes(li).insert(
                        kdb.Box(*(int(round(v * scale)) for v in b))
                    )

    return {"asap7_tap": _Asap7TapPCell}


def _build_asap7_pcells() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build every ASAP7 PCell (finfet + tap) for `gen.py`'s single hook."""
    from .asap7_finfet import _build_asap7_finfet_pcell

    return {**_build_asap7_finfet_pcell(), **_build_asap7_tap_pcell()}
