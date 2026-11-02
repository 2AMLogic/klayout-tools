"""Gate-level Verilog reference conversion plus the disclosure of its
case-insensitive pin binding (issue #4815); split out of `lvs` for the
file-size ratchet."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .lvs_mismatch import _mismatch
from .verilog_netlist import (
    convert_gate_level_verilog,
    convert_gate_level_verilog_expanded,
    parse_subckt_blocks,
)

#: Issue #4815: a `reference.form: "gate-level-verilog"` library-cell pin
#: connection was bound to the one resolved pin differing from it only by
#: letter case (Liberty-spelled synthesis output vs. CDL-spelled pin order).
CATEGORY_REFERENCE_GATE_LEVEL_PIN_CASE_FOLDED = "reference.gate_level_pin_case_folded"


def _gate_level_port_case_fold_warnings(
    folds: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Issue #4815: one ``severity: "warning"``
    :data:`CATEGORY_REFERENCE_GATE_LEVEL_PIN_CASE_FOLDED` entry per distinct
    (cell, Verilog port, resolved pin), with an instance count -- the
    disclosure for ``convert_gate_level_verilog``'s case-insensitive pin
    binding. Empty when nothing was folded (the common case)."""

    grouped: dict[tuple[str, str, str], int] = {}
    for fold in folds:
        key = (fold["cell"], fold["from"], fold["to"])
        grouped[key] = grouped.get(key, 0) + 1
    return [
        _mismatch(
            CATEGORY_REFERENCE_GATE_LEVEL_PIN_CASE_FOLDED,
            "warning",
            f"gate-level-verilog reference connects library cell '{cell}' "
            f"pin '{source}' on {count} instance(s), but the resolved pin "
            f"list spells it '{target}' -- bound case-insensitively "
            f"(unambiguous: no other pin of the cell differs from it only "
            f"by case) (see docs/cli/lvs.md, "
            '"reference.gate_level_pin_case_folded")',
            "reference",
            details={
                "cell": cell,
                "from": source,
                "to": target,
                "instance_count": count,
            },
        )
        for (cell, source, target), count in sorted(grouped.items())
    ]


def _lvs_error() -> type[Exception]:
    """`LvsError`, imported late (`lvs` imports this module)."""
    from .lvs import LvsError

    return LvsError


def _convert_gate_level_reference(
    text: str,
    pin_orders: Mapping[str, Any],
    finfet: bool,
    port_case_folds: list[dict[str, str]] | None = None,
) -> str:
    """Gate-level Verilog -> SPICE: pin-only stubs, or (issue #4462, FinFET
    deck extracting transistors with exact `nfin`) each library cell expanded
    to its CDL body."""
    if finfet:
        return convert_gate_level_verilog_expanded(
            text,
            pin_order_lookup=pin_orders.get,
            cell_body_lookup=_library_cell_body_lookup(pin_orders),
            port_case_folds=port_case_folds,
        )
    return convert_gate_level_verilog(
        text, pin_order_lookup=pin_orders.get, port_case_folds=port_case_folds
    )


def _library_cell_body_lookup(pin_orders: Mapping[str, Any]):
    """``cell -> '.SUBCKT .. .ENDS' text`` read from the file that declared
    the cell (issue #4462); each source file is parsed once."""
    sources: Mapping[str, str] = getattr(pin_orders, "cell_sources", {})
    parsed: dict[str, dict[str, str]] = {}

    def lookup(cell: str) -> str | None:
        path = sources.get(cell)
        if path is None:
            return None
        if path not in parsed:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    parsed[path] = parse_subckt_blocks(handle.read())
            except OSError as exc:
                raise _lvs_error()(
                    f"could not read library cell source '{path}': {exc}"
                ) from exc
        return parsed[path].get(cell)

    return lookup
