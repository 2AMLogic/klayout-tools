"""Post-flatten FinFET finger re-merge for ``klt lvs`` (issue #4954).

Issue #5630: this flatten + re-merge sequence is the v0.9.1 delta on the
documented routed-block recipe path (v0.9.0 flattened without the
``combine_devices()`` call), so the stage wrapper names it in any crash
traceback -- see :mod:`klayout_tools.lvs_crash_diagnostics`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

from .lvs_combine import _flatten_netlist_safely
from .lvs_crash_diagnostics import stage


@stage("flatten_layout_combine")
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
