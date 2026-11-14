"""``diode`` reference generator PCell for ``klt gen`` (issue #4300).

Geometry comes from :func:`klayout_tools.gen_diode._diode_layout`, shared with
the describe/validate callbacks in ``gen_describe.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb

#: Layer-role params, in declaration order; each maps to a hidden ``TypeLayer``
#: param named ``<role>_layer`` resolved by ``_diode_layer_params``.
DIODE_LAYER_ROLES = (
    "active",
    "tap",
    "nplus",
    "pplus",
    "well",
    "hvi",
    "diode_mark",
    "contact",
    "metal",
)


def _build_diode_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``diode`` generator's PCell -- a sky130 junction diode.

    Defined inside a function so importing ``klayout_tools.gen`` doesn't pay
    ``klayout.db``'s load cost until a generator runs (same discipline as the
    sibling ``_build_<family>_pcell`` factories).
    """
    import klayout.db as kdb

    from klayout_tools.gen import _insert_boxes
    from klayout_tools.gen_diode import _diode_layout

    class _DiodePCell(kdb.PCellDeclarationHelper):
        """Junction diode (issue #4300): ``kind`` selects n+/p-substrate
        (``n_psub``) or p+/nwell (``p_nwell``); ``voltage_class`` ``11v0``
        adds the ``hvi`` implant. Never draws ``areaid.ed`` (81/19)."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "kind",
                self.TypeString,  # type: ignore[attr-defined]
                "Junction: 'n_psub' (n+ in p-substrate, default) or "
                "'p_nwell' (p+ in nwell)",
                default="n_psub",
            )
            self.param(
                "voltage_class",
                self.TypeString,  # type: ignore[attr-defined]
                "'05v5' (default) or '11v0' (adds the hvi implant)",
                default="05v5",
            )
            self.param(
                "width_um",
                self.TypeDouble,  # type: ignore[attr-defined]
                "Junction diffusion width (um)",
                default=2.0,
            )
            self.param(
                "length_um",
                self.TypeDouble,  # type: ignore[attr-defined]
                "Junction diffusion length (um)",
                default=2.0,
            )
            for role in DIODE_LAYER_ROLES:
                self.param(
                    f"{role}_layer",
                    self.TypeLayer,  # type: ignore[attr-defined]
                    f"{role} drawing layer",
                    default=kdb.LayerInfo(0, 0),
                )

        def display_text_impl(self) -> str:
            return f"diode({self.kind},{self.voltage_class})"  # type: ignore[attr-defined]

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            info = _diode_layout(
                self.kind,  # type: ignore[attr-defined]
                self.voltage_class,  # type: ignore[attr-defined]
                self.width_um,  # type: ignore[attr-defined]
                self.length_um,  # type: ignore[attr-defined]
            )
            for role, boxes in info["boxes"].items():
                layer = getattr(self, f"{role}_layer")
                _insert_boxes(self.cell, self.layout.layer(layer), dbu, boxes)

    return {"diode": _DiodePCell}
