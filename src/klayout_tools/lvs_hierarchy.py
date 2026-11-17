"""``topology.hierarchy_mismatch`` advisory of ``klt lvs``.

The structural-asymmetry disclosure for a compared pair whose two sides sit
at different hierarchy levels, so the per-circuit ``topology`` "could not be
matched" errors it explains read as the structural finding they are instead
of a connectivity problem. Both directions (issue #4548's flat layout
against a hierarchical reference, and issue #5756's inverse -- a
hierarchical layout against a reference flattened by the tool's own
``options.flatten_reference``) live here. Split out of ``lvs_combine.py``
(issue #5756); the name is re-imported in ``lvs.py``, so
``klayout_tools.lvs`` keeps exposing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .lvs_mismatch import _mismatch

if TYPE_CHECKING:
    import klayout.db as kdb


def _hierarchy_mismatch_warnings(
    layout_netlist: kdb.Netlist,
    reference_netlist: kdb.Netlist,
    flatten_reference: bool | list[str],
) -> list[dict[str, Any]]:
    """Issues #4548/#5756: advisory ``topology.hierarchy_mismatch`` warnings.

    A flat layout netlist (``klt extract`` is flat-only) against a
    hierarchical reference reports only per-circuit ``topology`` "could not
    be matched" errors, which read like a connectivity problem. When the
    layout side is a single circuit, the reference still has more than one
    circuit (call after ``_prune_extra_top_circuits`` on both sides) and
    ``options.flatten_reference`` is not set, disclose that the cause is
    structural and name the option that compares the two (issue #4548,
    ``side: "reference"``).

    The inverse (issue #5756, ``side: "layout"``): a hierarchical layout
    netlist -- the normal shape for a routed block's extracted subcircuits
    -- against a reference that is a single circuit *because the tool
    itself flattened it* (``options.flatten_reference`` set; it defaults to
    true for a gate-level-verilog reference on a FinFET deck) reports the
    same generic per-circuit errors. When ``flatten_reference`` is set, the
    layout side still has more than one circuit and the reference side has
    exactly one, disclose the asymmetry and name the option that compares
    the two. A list ``flatten_reference`` that kept stub circuits leaves the
    reference hierarchical, so that shape is not flagged.

    Called after both sides' optional flatten, so the netlists already
    reflect the post-flatten state: ``options.flatten_layout: true`` leaves
    the layout side a single circuit and this warning cannot fire. Returns
    a one-entry list, else an empty one. Warning-only: never changes
    ``status``.
    """

    from .lvs import CATEGORY_TOPOLOGY_HIERARCHY_MISMATCH

    layout_circuits = sum(1 for _ in layout_netlist.each_circuit())
    reference_circuits = sum(1 for _ in reference_netlist.each_circuit())
    if not flatten_reference:
        # Issue #4548: flat layout against a still-hierarchical reference.
        if layout_circuits != 1 or reference_circuits <= 1:
            return []
        return [
            _mismatch(
                CATEGORY_TOPOLOGY_HIERARCHY_MISMATCH,
                "warning",
                f"the layout netlist is flat (1 circuit) but the reference netlist is "
                f"hierarchical ({reference_circuits} circuits); per-circuit topology "
                "errors here may stem from that structural difference rather than a "
                "connectivity defect -- retry with options.flatten_reference: true to "
                "compare the flattened reference against the flat layout "
                '(see docs/cli/lvs.md, "topology.hierarchy_mismatch")',
                "reference",
            )
        ]
    # Issue #5756: hierarchical layout against a reference the tool itself
    # flattened to a single circuit.
    if layout_circuits <= 1 or reference_circuits != 1:
        return []
    return [
        _mismatch(
            CATEGORY_TOPOLOGY_HIERARCHY_MISMATCH,
            "warning",
            f"the layout netlist is hierarchical ({layout_circuits} circuits) but "
            "the reference netlist is a single circuit because "
            "options.flatten_reference flattened it; per-circuit topology errors "
            "here may stem from that structural difference rather than a "
            "connectivity defect -- retry with options.flatten_layout: true to "
            "compare the flattened layout against the flattened reference "
            '(see docs/cli/lvs.md, "topology.hierarchy_mismatch")',
            "layout",
        )
    ]
