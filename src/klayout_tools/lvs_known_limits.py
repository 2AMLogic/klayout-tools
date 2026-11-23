"""In-band disclosure of pinned ASAP7 library limitations (issues #4819,
#6931)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .lvs_mismatch import _mismatch, _sort_key

#: Issue #4819: a mismatching FinFET compare whose top cell is a pinned
#: unsupported ASAP7 cell (always a warning; never changes ``status``).
CATEGORY_TOPOLOGY_KNOWN_LIMITATION = "topology.known_limitation"

#: Issue #4819: ASAP7 RVT library cells whose drawn stack topology differs from
#: the library CDL (11 folded-stack, 6 series-order). They stay
#: ``status: "mismatch"`` -- nothing is turned green -- and ``klt lvs`` adds a
#: ``topology.known_limitation`` warning naming the cell so the signal is
#: in-band. Pinned against the corpus in ``tests/test_asap7_finfet.py``.
KNOWN_UNSUPPORTED_ASAP7_CELLS: dict[str, str] = {
    **{
        f"{name}_ASAP7_75t_R": "folded-stack"
        for name in (
            "AND2x4 AND2x6 AOI21x1 AOI22x1 NAND3x2 NOR3x2 OAI21x1 OAI22x1 "
            "OR2x6 XNOR2x1 XOR2x1"
        ).split()
    },
    **{
        f"{name}_ASAP7_75t_R": "series-order"
        for name in "A2O1A1O1Ixp25 AOI211xp5 OAI221xp5 SDFLx1 SDFLx2 SDFLx3".split()
    },
}

#: Issue #6931: case-insensitive lookup onto the canonical spelling above --
#: ``NetlistSpiceReader`` upper-cases every ``.subckt``/``X``-card name, so a
#: reference circuit read from SPICE carries ``AOI21X1_ASAP7_75T_R`` while
#: the report must still name the library's own mixed-case spelling.
_KNOWN_BY_UPPER: dict[str, tuple[str, str]] = {
    name.upper(): (name, kind) for name, kind in KNOWN_UNSUPPORTED_ASAP7_CELLS.items()
}


def count_unsupported_asap7_instances(netlist: Any, top: str | None) -> dict[str, int]:
    """Issue #6931: instance count per canonical pinned unsupported ASAP7
    master reachable **beneath** the compared top, as ``{<canonical cell
    name>: <instance count>}`` (empty when nothing intersects the pinned
    list).

    ``netlist`` is walked while it still carries its hierarchy: one
    ``SubCircuit`` per ``X`` card on a SPICE-read side, one per cell
    placement on an extracted side (verified against KLayout 0.30's
    ``LayoutToNetlist``: two placements of one cell yield two subcircuit
    entries). Counts multiply through the hierarchy -- a wrapper placed twice
    containing one pinned leaf contributes 2 -- and a pinned master that is
    only *declared* (a library ``.subckt`` with no caller, or a GDS cell not
    placed under the top) contributes nothing. Circuit names are matched
    case-insensitively and reported in the canonical library spelling.

    ``top`` resolves like :func:`klayout_tools.lvs._select_circuit` (by name,
    else the netlist's sole top circuit) but resolves to "no root" -- ``{}``
    -- instead of raising: the authoritative error for an unresolvable top
    comes from circuit selection itself, later, and this census must never
    pre-empt it."""
    root = None
    if top is not None:
        root = netlist.circuit_by_name(top)
    else:
        top_circuits = list(netlist.top_circuits())
        if len(top_circuits) == 1:
            root = top_circuits[0]
    if root is None:
        return {}
    # KLayout visits all callers before their children. Seed only the selected
    # root so additional top circuits and unused definitions contribute nothing.
    placements: Counter[Any] = Counter({root: 1})
    unsupported: Counter[str] = Counter()
    for circuit in netlist.each_circuit_top_down():
        count = placements[circuit]
        if not count:
            continue
        for instance in circuit.each_subcircuit():
            child = instance.circuit_ref()
            if child is None:
                continue
            placements[child] += count
            pinned = _KNOWN_BY_UPPER.get(child.name.upper())
            if pinned is not None:
                unsupported[pinned[0]] += count
    return dict(unsupported)


def note_known_limit(
    mismatches: list[dict[str, Any]],
    finfet_layout: bool,
    status: str,
    *specs: dict[str, Any],
    layout_counts: dict[str, int] | None = None,
    reference_counts: dict[str, int] | None = None,
) -> None:
    """Append one ``severity: "warning"`` ``topology.known_limitation`` entry
    to ``mismatches`` (re-sorted) when a mismatching FinFET compare's top cell
    (reference or layout spec) is a pinned unsupported ASAP7 cell, plus one
    entry per pinned master *instantiated* beneath either selected top (issue
    #6931: ``layout_counts``/``reference_counts`` are
    :func:`count_unsupported_asap7_instances` censuses taken before each
    side's optional flatten). Never changes ``status``; no-op otherwise."""
    if not (finfet_layout and status == "mismatch"):
        return
    appended = False
    for spec in specs:
        cell = spec.get("top") or ""
        kind = KNOWN_UNSUPPORTED_ASAP7_CELLS.get(cell)
        if kind:
            mismatches.append(
                _mismatch(
                    CATEGORY_TOPOLOGY_KNOWN_LIMITATION,
                    "warning",
                    f"cell '{cell}' is a known ASAP7 RVT library limitation: its "
                    f"drawn {kind} transistor topology differs from the library "
                    "CDL and klt lvs performs no stack normalisation, so this "
                    "mismatch is expected for the cell, not an extraction defect "
                    "(docs/cli/lvs.md, 'Pinned ASAP7 RVT corpus')",
                    "layout",
                    details={"cell": cell, "limitation": kind},
                )
            )
            appended = True
            break
    for cell in sorted(set(layout_counts or {}) | set(reference_counts or {})):
        kind = KNOWN_UNSUPPORTED_ASAP7_CELLS.get(cell)
        if kind is None:
            continue
        layout_n = (layout_counts or {}).get(cell, 0)
        reference_n = (reference_counts or {}).get(cell, 0)
        if layout_n <= 0 and reference_n <= 0:
            continue
        mismatches.append(
            _mismatch(
                CATEGORY_TOPOLOGY_KNOWN_LIMITATION,
                "warning",
                f"block instantiates {layout_n} layout and {reference_n} "
                f"reference instance(s) of cell '{cell}', a known ASAP7 RVT "
                f"library limitation: its drawn {kind} transistor topology "
                "differs from the library CDL and klt lvs performs no stack "
                "normalisation, so those instances are expected to mismatch "
                "-- a possible contributor to this block-level mismatch, not "
                "an extraction defect (docs/cli/lvs.md, 'Pinned ASAP7 RVT "
                "corpus')",
                "both",
                details={
                    "cell": cell,
                    "limitation": kind,
                    "layout_count": layout_n,
                    "reference_count": reference_n,
                },
            )
        )
        appended = True
    if appended:
        mismatches.sort(key=_sort_key)
