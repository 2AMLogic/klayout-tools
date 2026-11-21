"""Post-flatten FinFET finger re-merge for ``klt lvs`` (issue #4954)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

from .lvs_combine import _flatten_netlist_safely


def flatten_layout_netlist(
    netlist: kdb.Netlist, finfet: bool, warnings: list[dict[str, Any]]
) -> None:
    """Flatten the layout netlist (appending any flatten warning to
    ``warnings``); for a FinFET layout re-merge the exactly-parallel fingers the
    flatten brought together (the extractor only merges within one circuit)."""
    warning = _flatten_netlist_safely(netlist, "layout")
    if warning is not None:
        warnings.append(warning)
    if finfet:
        netlist.combine_devices()
