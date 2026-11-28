"""``pdk_cell`` reference generator PCell for ``klt gen`` (issue #5290).

Arrays one of a PDK's fixed, pre-drawn GDS cells. The geometry is copied from
the PDK GDS at produce time (never vendored); request validation, cell lookup
and port reporting live in :mod:`klayout_tools.gen_pdk_cell`. The PCell is the
fixed-cell array ``cap_vpp_array`` shares, plus this generator's ``library``
parameter (issue #8081).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

from .cap_vpp_array import _build_fixed_cell_array_pcell


def _declare_library(pcell: Any) -> None:
    """Declare ``pdk_cell``'s leading ``library`` parameter."""
    pcell.param(
        "library",
        pcell.TypeString,
        "PDK library directory under libs.ref (e.g. sg13g2_io)",
        default="",
    )


def _build_pdk_cell_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``pdk_cell`` PCell -- a row of one PDK fixed cell."""
    return _build_fixed_cell_array_pcell(
        "pdk_cell",
        pdk_cell_desc="PDK fixed cell name",
        pdk_cell_default="",
        num_default=1,
        extra_params=_declare_library,
    )
