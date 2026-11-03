"""In-band disclosure of pinned ASAP7 library limitations (issue #4819)."""

from __future__ import annotations

from typing import Any

from .lvs_mismatch import _mismatch, _sort_key

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


def note_known_limit(
    mismatches: list[dict[str, Any]],
    finfet_layout: bool,
    status: str,
    *specs: dict[str, Any],
) -> None:
    """Append one ``severity: "warning"`` ``topology.known_limitation`` entry
    to ``mismatches`` (re-sorted) when a mismatching FinFET compare's top cell
    (reference or layout spec) is a pinned unsupported ASAP7 cell. Never
    changes ``status``; no-op otherwise."""
    if not (finfet_layout and status == "mismatch"):
        return
    for spec in specs:
        cell = spec.get("top") or ""
        kind = KNOWN_UNSUPPORTED_ASAP7_CELLS.get(cell)
        if kind:
            mismatches.append(
                _mismatch(
                    "topology.known_limitation",
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
            mismatches.sort(key=_sort_key)
            return
