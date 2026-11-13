"""Constraint arcs for sequential cells in ``klt characterize``: setup/hold
on data pins (issue #4756) and recovery/removal on asynchronous set/reset
pins (issue #5520) -- request validation, case derivation, table assembly
and the JSON/Liberty shapes. The probe decks and bisection live in
:mod:`klayout_tools.characterize_bisect`; the async *propagation* arcs those
recovery/removal cases are derived from live in
:mod:`klayout_tools.characterize_async`.
"""

from __future__ import annotations

from typing import Any

from . import liberty_writer
from ._jsonnum import is_number
from .characterize_async import run_async
from .characterize_bisect import (
    ASYNC_KINDS,
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
        or any(not is_number(v) for v in window)
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


def derive_async_cases(
    cell: dict[str, Any], async_entries: list[dict[str, Any]]
) -> tuple[ConstraintCase, ...]:
    """The recovery and removal cases of each declared async pin
    (issue #5520).

    ``async_entries`` are the *measured* propagation arcs
    (:mod:`klayout_tools.characterize_async`), so the forced level each pin
    drives an output to is known rather than assumed. One output per pin is
    enough to observe the capture, and the first measured one is used: the
    probe holds the data pins at the state whose capture drives that output
    the **other** way, asserts the async pin from t=0 and releases it at the
    bisected offset from the clock edge.
    """
    chosen: dict[str, dict[str, Any]] = {}
    for entry in async_entries:
        chosen.setdefault(entry["case"].pin, entry)
    cases: list[ConstraintCase] = []
    for spec in cell["async_pins"]:
        measured = chosen.get(spec.pin)
        if measured is None:
            raise ConstraintError(
                f"cell '{cell['name']}': async pin '{spec.pin}' forces no "
                "output, so its recovery/removal cannot be observed"
            )
        forced_rising = measured["out_rising"]
        case = measured["case"]
        for kind in ASYNC_KINDS:
            cases.append(
                ConstraintCase(
                    pin=spec.pin,
                    # The pin is released, so its edge goes to the inactive
                    # level: a rise for an active-low pin.
                    edge="rise" if spec.inactive_level else "fall",
                    kind=kind,
                    output_pin=case.output_pin,
                    others=case.state(forced_rising),
                    start_level=spec.active_level,
                    captured_level=spec.inactive_level,
                    # The clock captures the opposite of the forced level.
                    out_rising=not forced_rising,
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
    """Group the tables into ``{pin: (ConstraintArc, ...)}``.

    A data pin carries its setup and hold arcs (both edges in one group); an
    async pin carries recovery and removal, and because only its *release*
    edge is constrained each of those groups holds a single table
    (``rise_constraint`` for an active-low pin, ``fall_constraint`` for an
    active-high one).
    """
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
            for kind in ("setup", "hold", *ASYNC_KINDS)
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
                # Removal passes on the *absence* of an output transition,
                # so it has no reference delay to degrade: both fields are
                # null rather than a number that means nothing.
                "reference_delay_ns": (
                    None
                    if case.kind == "removal"
                    else [
                        [outcome["reference_ns"][(c, ci, di)] for ci in range(n)]
                        for di in range(n)
                    ]
                ),
                "degradation": (
                    None if case.kind == "removal" else options["degradation"]
                ),
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
    async_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Characterize every constrained pin of ``cell``, and the recovery and
    removal of every declared async pin.

    Returns ``{"liberty": {pin: (ConstraintArc, ...)}, "entries": [...]}``:
    the Liberty groups and the JSON ``constraints[]`` list. Both pin kinds
    share one lockstep bisection, so an async pin costs no extra simulation
    rounds beyond the ones setup/hold already pays for.
    """
    cases = derive_cases(cell, arcs)
    if async_entries:
        cases = cases + derive_async_cases(cell, async_entries)
    if not cases:
        return {"liberty": {}, "entries": []}
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


def run_sequential_constraints(
    *,
    cell: dict[str, Any],
    arcs: tuple[Any, ...],
    grid: dict[str, tuple[float, ...]],
    rail_nodes: dict[str, str],
    vdd: float,
    thresholds: liberty_writer.Thresholds,
    ramps_ns: tuple[float, ...],
    settle_ns: float,
    options: dict[str, Any],
    run_deck: DeckRunner,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Both opt-in sequential measurements of one cell, in the order they
    depend on each other: ``(async_arcs, constraints)``.

    The async propagation arcs come first because they measure which way
    each declared pin forces each output, which is what the recovery/removal
    cases hold the data pins against. Either half is ``None`` when the
    request did not ask for it.
    """
    async_arcs = None
    if cell["async_pins"]:
        async_arcs = run_async(
            cell=cell,
            arcs=arcs,
            grid_ns=grid["input_transition_ns"],
            loads_pf=grid["output_load_pf"],
            rail_nodes=rail_nodes,
            vdd=vdd,
            thresholds=thresholds,
            ramps_ns=ramps_ns,
            settle_ns=settle_ns,
            run_deck=run_deck,
        )
    constraints = None
    if cell["constrained_pins"] or cell["async_pins"]:
        constraints = run_constraints(
            cell=cell,
            arcs=arcs,
            grid_ns=grid["input_transition_ns"],
            rail_nodes=rail_nodes,
            vdd=vdd,
            thresholds=thresholds,
            ramps_ns=ramps_ns,
            settle_ns=settle_ns,
            load_pf=min(grid["output_load_pf"]),
            options=options,
            run_deck=run_deck,
            async_entries=(async_arcs or {}).get("measured"),
        )
    return async_arcs, constraints
