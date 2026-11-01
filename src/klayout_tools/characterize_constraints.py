"""Setup/hold constraint arcs for sequential cells in ``klt characterize``
(issue #4756): request validation, case derivation, table assembly and the
JSON/Liberty shapes. The probe decks and bisection live in
:mod:`klayout_tools.characterize_bisect`.
"""

from __future__ import annotations

from typing import Any

from . import liberty_writer
from .characterize_bisect import (
    ConstraintCase,
    ConstraintError,
    DeckRunner,
    run_bisection,
)

DEFAULT_DEGRADATION = 0.1
DEFAULT_WINDOW_NS = (-1.0, 2.0)
DEFAULT_TOLERANCE_NS = 0.002


def resolve_constrained_pins(
    spec: dict[str, Any],
    pins: tuple[dict[str, Any], ...],
    clock_pin: str | None,
    where: str,
) -> tuple[str, ...]:
    """Validate ``cell.constrained_pins`` (a non-empty list of data inputs)."""
    raw = spec.get("constrained_pins")
    if raw is None:
        return ()
    if clock_pin is None:
        raise ConstraintError(f"{where}.constrained_pins requires {where}.clock_pin")
    if not isinstance(raw, list) or not raw:
        raise ConstraintError(
            f"{where}.constrained_pins must be a non-empty array of input pin names"
        )
    inputs = {pin["name"] for pin in pins if pin["direction"] == "input"}
    seen: list[str] = []
    for name in raw:
        if not isinstance(name, str) or name not in inputs or name == clock_pin:
            raise ConstraintError(
                f"{where}.constrained_pins names {name!r}, which is not a "
                "declared non-clock input pin"
            )
        if name in seen:
            raise ConstraintError(f"{where}.constrained_pins lists {name!r} twice")
        seen.append(name)
    return tuple(seen)


def resolve_constraint_options(spec: dict[str, Any]) -> dict[str, Any]:
    """``options.constraint_degradation`` / ``constraint_window_ns`` /
    ``constraint_tolerance_ns``."""
    degradation = spec.get("constraint_degradation", DEFAULT_DEGRADATION)
    if (
        isinstance(degradation, bool)
        or not isinstance(degradation, (int, float))
        or not 0 < degradation <= 1
    ):
        raise ConstraintError(
            "request.options.constraint_degradation must be a number in (0, 1]"
        )
    window = spec.get("constraint_window_ns", list(DEFAULT_WINDOW_NS))
    if (
        not isinstance(window, list)
        or len(window) != 2
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in window)
        or not window[0] < 0 < window[1]
    ):
        raise ConstraintError(
            "request.options.constraint_window_ns must be [low, high] "
            "with low < 0 < high"
        )
    tolerance = spec.get("constraint_tolerance_ns", DEFAULT_TOLERANCE_NS)
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or tolerance <= 0
    ):
        raise ConstraintError(
            "request.options.constraint_tolerance_ns must be a positive number"
        )
    return {
        "degradation": float(degradation),
        "window_ns": (float(window[0]), float(window[1])),
        "tolerance_ns": float(tolerance),
    }


def derive_cases(
    cell: dict[str, Any], arcs: tuple[Any, ...]
) -> tuple[ConstraintCase, ...]:
    """The four (edge, kind) cases of each constrained pin.

    The pin's polarity into the output comes from the clock-to-output arc
    whose high/low next-state assignments differ in exactly that pin.
    """
    base = {p: False for p in cell["input_pins"] if p != cell["clock_pin"]}
    cases: list[ConstraintCase] = []
    for pin in cell["constrained_pins"]:
        found = None
        for arc in arcs:
            high = {**base, **dict(arc.side_inputs)}
            low = {**base, **dict(arc.fall_side_inputs)}
            if high[pin] != low[pin] and all(
                high[n] == low[n] for n in base if n != pin
            ):
                found = (arc, high)
                break
        if found is None:
            raise ConstraintError(
                f"cell '{cell['name']}': constrained pin '{pin}' has no output "
                "whose next-state function toggles with that pin alone, so its "
                "capture cannot be observed"
            )
        arc, high = found
        positive = high[pin]
        others = tuple((n, v) for n, v in sorted(high.items()) if n != pin)
        for edge in ("rise", "fall"):
            new = edge == "rise"
            for kind in ("setup", "hold"):
                captured = new if kind == "setup" else not new
                cases.append(
                    ConstraintCase(
                        pin=pin,
                        edge=edge,
                        kind=kind,
                        output_pin=arc.output_pin,
                        others=others,
                        start_level=not captured,
                        captured_level=captured,
                        out_rising=(captured == positive),
                    )
                )
    return tuple(cases)


def build_tables(
    cases: tuple[ConstraintCase, ...],
    grid_ns: tuple[float, ...],
    outcome: dict[str, Any],
) -> list[dict[str, Any]]:
    """One entry per case: ``{"case", "table"}`` with data-slew rows x
    clock-slew columns (Liberty's ``constrained_pin_transition`` x
    ``related_pin_transition``)."""
    entries = []
    n = len(grid_ns)
    for c, case in enumerate(cases):
        rows = tuple(
            tuple(outcome["values"][(c, ci, di)] for ci in range(n)) for di in range(n)
        )
        entries.append(
            {
                "case": case,
                "table": liberty_writer.Table2D(
                    index_1=grid_ns, index_2=grid_ns, values=rows
                ),
            }
        )
    return entries


def liberty_constraints(
    entries: list[dict[str, Any]], clock_pin: str, clock_edge: str
) -> dict[str, tuple[liberty_writer.ConstraintArc, ...]]:
    """Group the tables into ``{pin: (setup arc, hold arc)}``."""
    suffix = "rising" if clock_edge == "rising" else "falling"
    by_pin: dict[str, dict[str, dict[str, liberty_writer.Table2D]]] = {}
    for entry in entries:
        case = entry["case"]
        by_pin.setdefault(case.pin, {}).setdefault(case.kind, {})[case.edge] = entry[
            "table"
        ]
    result = {}
    for pin, kinds in by_pin.items():
        result[pin] = tuple(
            liberty_writer.ConstraintArc(
                related_pin=clock_pin,
                timing_type=f"{kind}_{suffix}",
                rise_constraint=kinds[kind].get("rise"),
                fall_constraint=kinds[kind].get("fall"),
            )
            for kind in ("setup", "hold")
            if kind in kinds
        )
    return result


def response_entries(
    entries: list[dict[str, Any]],
    outcome: dict[str, Any],
    clock_pin: str,
    clock_edge: str,
    options: dict[str, Any],
) -> list[dict[str, Any]]:
    """The JSON ``constraints[]`` list: one object per (pin, edge, kind)."""
    suffix = "rising" if clock_edge == "rising" else "falling"
    out = []
    for c, entry in enumerate(entries):
        case, table = entry["case"], entry["table"]
        n = len(table.index_1)
        out.append(
            {
                "pin": case.pin,
                "related_pin": clock_pin,
                "output_pin": case.output_pin,
                "kind": case.kind,
                "data_edge": case.edge,
                "timing_type": f"{case.kind}_{suffix}",
                "constraint": {
                    "index_1": list(table.index_1),
                    "index_2": list(table.index_2),
                    "values": [list(row) for row in table.values],
                },
                "reference_delay_ns": [
                    [outcome["reference_ns"][(c, ci, di)] for ci in range(n)]
                    for di in range(n)
                ],
                "degradation": options["degradation"],
                "tolerance_ns": options["tolerance_ns"],
                "iterations": outcome["iterations"],
            }
        )
    return out


def run_constraints(
    *,
    cell: dict[str, Any],
    arcs: tuple[Any, ...],
    grid_ns: tuple[float, ...],
    rail_nodes: dict[str, str],
    vdd: float,
    thresholds: liberty_writer.Thresholds,
    ramps_ns: tuple[float, ...],
    settle_ns: float,
    load_pf: float,
    options: dict[str, Any],
    run_deck: DeckRunner,
) -> dict[str, Any]:
    """Characterize every constrained pin of ``cell``.

    Returns ``{"liberty": {pin: (ConstraintArc, ...)}, "entries": [...]}``:
    the Liberty groups and the JSON ``constraints[]`` list.
    """
    cases = derive_cases(cell, arcs)
    outcome = run_bisection(
        cell=cell,
        cases=cases,
        grid_ns=grid_ns,
        rail_nodes=rail_nodes,
        vdd=vdd,
        thresholds=thresholds,
        ramps_ns=ramps_ns,
        settle_ns=settle_ns,
        load_pf=load_pf,
        options=options,
        run_deck=run_deck,
    )
    tables = build_tables(cases, grid_ns, outcome)
    return {
        "liberty": liberty_constraints(tables, cell["clock_pin"], cell["clock_edge"]),
        "entries": response_entries(
            tables, outcome, cell["clock_pin"], cell["clock_edge"], options
        ),
    }
