"""Probe-deck generation and lockstep bisection for setup/hold constraint
arcs (issue #4756); the request-facing API is
:mod:`klayout_tools.characterize_constraints`.

A constraint is measured by **bisection over data-vs-clock separation**. Each
probe is one cell instance whose data pin makes a single transition at a
chosen offset from the clock's active edge; the probe *passes* when the
cell's output still makes the expected transition and its clock-to-output
delay is within ``(1 + degradation)`` of the reference delay measured with the
data edge far from the clock (the window's wide end). The constraint is the
smallest passing offset, to within ``tolerance_ns``.

- **setup**: the data pin switches to the value the clock edge must capture,
  ``s`` before the clock edge.
- **hold**: the data pin holds the value to be captured through the clock
  edge and switches away ``h`` after it.

Every probe of an iteration lives in one deck (one ``klt sim`` run), bisected
in lockstep across all points, so the cost is
``2 + ceil(log2(window / tolerance))`` simulations. A point whose bracket does
not contain a boundary raises :class:`ConstraintError` -- never a fabricated
value.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import liberty_writer
from .characterize_sequential import _spice_number

#: Runs one probe deck: ``(netlist_text, measurements, label, stop_ns) ->
#: {measurement name: value in seconds, or None when ngspice found no
#: crossing}``.
DeckRunner = Callable[[str, list[dict[str, Any]], str, float], dict[str, Any]]


class ConstraintError(Exception):
    """Invalid constraint request, or a bisection that cannot be trusted."""


@dataclass(frozen=True)
class ConstraintCase:
    """One (constrained pin, data edge, kind) combination."""

    pin: str
    edge: str  # "rise" / "fall": the constrained pin's transition
    kind: str  # "setup" / "hold"
    output_pin: str
    #: Levels of every other non-clock input during the probe.
    others: tuple[tuple[str, bool], ...]
    #: Pin level before the clock edge captures (what the prime pulse stores).
    start_level: bool
    #: Pin level the clock edge captures.
    captured_level: bool
    #: Direction of the output edge the capture produces.
    out_rising: bool


def _frac(rising: bool, thresholds: liberty_writer.Thresholds) -> float:
    """Where along a full-swing linear ramp the input threshold is crossed."""
    if rising:
        return thresholds.input_threshold_pct_rise / 100.0
    return 1.0 - thresholds.input_threshold_pct_fall / 100.0


def _pwl(name: str, node: str, points: list[tuple[float, float]]) -> str:
    body = " ".join(f"{_spice_number(t)}n {_spice_number(v)}" for t, v in points)
    return f"{name} {node} 0 PWL({body})"


def _schedule(settle: float, max_ramp: float, window: tuple[float, float]):
    pulse_w = max_ramp + settle
    prime = settle
    prime_end = prime + pulse_w + max_ramp
    reach = max(-window[0], window[1])
    t_early = prime_end + settle
    t_clk = prime_end + 2 * settle + 3 * max_ramp + reach
    stop = t_clk + pulse_w + max_ramp + max(window[1], 0.0) + settle
    return prime, pulse_w, t_early, t_clk, stop


def build_deck(
    *,
    cell: dict[str, Any],
    cases: tuple[ConstraintCase, ...],
    probes: list[tuple[int, int, int, float]],
    rail_nodes: dict[str, str],
    vdd: float,
    thresholds: liberty_writer.Thresholds,
    ramps_ns: tuple[float, ...],
    settle_ns: float,
    window_ns: tuple[float, float],
    load_pf: float,
    edge: str,
) -> tuple[str, list[dict[str, Any]], float]:
    """One deck of ``probes`` -- ``(case index, clock slew index, data slew
    index, offset_ns)`` -- and the ``.meas`` card that reads each one's
    clock-to-output delay. Returns ``(netlist_text, measurements, stop_ns)``.
    """
    max_ramp = max(ramps_ns)
    prime, pulse_w, t_early, t_clk, stop = _schedule(settle_ns, max_ramp, window_ns)
    idle, active = (0.0, vdd) if edge == "rising" else (vdd, 0.0)
    clk_rising = edge == "rising"
    lines = [
        "* klt characterize -- setup/hold constraint probes, do not edit",
        f".include {cell['netlist_path']}",
        f"Vdd vdd 0 DC {_spice_number(vdd)}",
    ]
    measurements: list[dict[str, Any]] = []
    for k, (case_index, ci, di, offset) in enumerate(probes):
        case = cases[case_index]
        rc, rd = ramps_ns[ci], ramps_ns[di]
        clk50 = t_clk + rc * _frac(clk_rising, thresholds)
        d_rising = case.edge == "rise"
        new_level = vdd if d_rising else 0.0
        old_level = vdd - new_level
        d50 = clk50 + (offset if case.kind == "hold" else -offset)
        t_start = d50 - rd * _frac(d_rising, thresholds)
        clk_points = [(0.0, idle)]
        for start in (prime, t_clk):
            clk_points += [
                (start, idle),
                (start + rc, active),
                (start + pulse_w, active),
                (start + pulse_w + rc, idle),
            ]
        if case.kind == "setup":
            d_points = [
                (0.0, old_level),
                (t_start, old_level),
                (t_start + rd, new_level),
            ]
        else:
            d_points = [
                (0.0, new_level),
                (t_early, new_level),
                (t_early + max_ramp, old_level),
                (t_start, old_level),
                (t_start + rd, new_level),
            ]
        tag = f"c{k}"
        node_of: dict[str, str] = {
            cell["clock_pin"]: f"ck_{tag}",
            case.pin: f"dd_{tag}",
        }
        for name, value in case.others:
            node_of[name] = "vdd" if value else "0"
        for name in cell["output_pins"]:
            node_of[name] = f"o_{tag}_{name}"
        nodes = [node_of.get(t) or rail_nodes[t] for t in cell["terminals"]]
        lines.append(_pwl(f"Vk{tag}", f"ck_{tag}", clk_points))
        lines.append(_pwl(f"Vd{tag}", f"dd_{tag}", d_points))
        lines.append(f"X{tag} {' '.join(nodes)} {cell['subckt']}")
        for name in cell["output_pins"]:
            lines.append(f"C{tag}_{name} {node_of[name]} 0 {_spice_number(load_pf)}p")
        out_thr = (
            thresholds.output_threshold_pct_rise
            if case.out_rising
            else thresholds.output_threshold_pct_fall
        )
        clk_thr = (
            thresholds.input_threshold_pct_rise
            if clk_rising
            else thresholds.input_threshold_pct_fall
        )
        td = f"TD={_spice_number(t_clk)}n"
        measurements.append(
            {
                "name": f"{tag}_d",
                "spice": (
                    f".meas tran {tag}_d TRIG v(ck_{tag}) "
                    f"VAL={_spice_number(vdd * clk_thr / 100.0)} "
                    f"{'RISE' if clk_rising else 'FALL'}=1 {td} "
                    f"TARG v({node_of[case.output_pin]}) "
                    f"VAL={_spice_number(vdd * out_thr / 100.0)} "
                    f"{'RISE' if case.out_rising else 'FALL'}=1 {td}"
                ),
                "unit": "s",
            }
        )
    return "\n".join(lines) + "\n", measurements, stop


def _where(case: ConstraintCase, ci: int, di: int, grid: tuple[float, ...]) -> str:
    return (
        f"{case.kind} on '{case.pin}' ({case.edge} data edge) at clock "
        f"transition {grid[ci]} ns / data transition {grid[di]} ns"
    )


def run_bisection(
    *,
    cell: dict[str, Any],
    cases: tuple[ConstraintCase, ...],
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
    """Bisect every (case, clock slew, data slew) point in lockstep.

    Returns ``{"values": {(case, ci, di): offset_ns}, "reference_ns":
    {...}, "iterations": n}``. Raises :class:`ConstraintError` when a point's
    bracket does not contain a boundary.
    """
    wlo, whi = options["window_ns"]
    degradation = options["degradation"]
    points = [
        (c, ci, di)
        for c in range(len(cases))
        for ci in range(len(grid_ns))
        for di in range(len(grid_ns))
    ]

    def run(probes: list[tuple[int, int, int, float]], label: str):
        text, measurements, stop = build_deck(
            cell=cell,
            cases=cases,
            probes=probes,
            rail_nodes=rail_nodes,
            vdd=vdd,
            thresholds=thresholds,
            ramps_ns=ramps_ns,
            settle_ns=settle_ns,
            window_ns=options["window_ns"],
            load_pf=load_pf,
            edge=cell["clock_edge"],
        )
        raw = run_deck(text, measurements, label, stop)
        return [raw.get(f"c{k}_d") for k in range(len(probes))]

    probes = [(c, ci, di, whi) for c, ci, di in points] + [
        (c, ci, di, wlo) for c, ci, di in points
    ]
    results = run(probes, "bracket")
    wide, narrow = results[: len(points)], results[len(points) :]
    reference: dict[tuple[int, int, int], float] = {}
    for point, ref, low in zip(points, wide, narrow, strict=True):
        where = _where(cases[point[0]], point[1], point[2], grid_ns)
        if ref is None:
            raise ConstraintError(
                f"{where}: the output never made the expected transition even "
                f"with the data edge {whi} ns from the clock edge -- the "
                "constraint cannot be bracketed (widen "
                "options.constraint_window_ns, or check the cell's function/"
                "static_inputs)"
            )
        if low is not None and low <= ref * (1.0 + degradation):
            raise ConstraintError(
                f"{where}: the cell still captures with the data edge {wlo} ns "
                "from the clock edge, so the boundary lies below the search "
                "window (lower options.constraint_window_ns[0])"
            )
        reference[point] = ref

    low_side = {p: wlo for p in points}
    high_side = {p: whi for p in points}
    iterations = max(1, math.ceil(math.log2((whi - wlo) / options["tolerance_ns"])))
    for n in range(iterations):
        mids = {p: 0.5 * (low_side[p] + high_side[p]) for p in points}
        measured = run(
            [(c, ci, di, mids[(c, ci, di)]) for c, ci, di in points], f"iter{n}"
        )
        for point, delay in zip(points, measured, strict=True):
            passed = delay is not None and delay <= reference[point] * (
                1.0 + degradation
            )
            if passed:
                high_side[point] = mids[point]
            else:
                low_side[point] = mids[point]
    return {
        "values": dict(high_side),
        "reference_ns": {p: v * 1e9 for p, v in reference.items()},
        "iterations": iterations,
    }
