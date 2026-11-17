"""Asynchronous set/reset propagation arcs for ``klt characterize``
(issue #5520): the ``preset``/``clear`` ``timing()`` groups an async pin
carries into each output it forces.

The recovery/removal *constraint* groups of the same pin ride the existing
setup/hold bisection (:mod:`klayout_tools.characterize_bisect`,
:mod:`klayout_tools.characterize_constraints`); only the propagation arcs
live here, because they are an ordinary delay/transition measurement on a
grid rather than a search.

**The forced direction is measured, not inferred.** A request declares what
an async pin does to the cell's *stored state* (``"clear"`` -> 0,
``"preset"`` -> 1); which way a given output pin then moves depends on
whether that output follows the state or its complement, and the request
does not say. Both are probed: one instance is primed (through the clock)
with the output **high**, one with it **low**, and the pin is asserted in
each. Exactly one of the two produces an output edge -- the other is already
at the forced level, so its ``.meas`` finds no crossing. Which one answered
*is* the forced direction, so ``timing_type`` follows the output
(``preset`` where the output is driven high, ``clear`` where it is driven
low) with nothing guessed: a clear pin gives ``clear`` on ``Q`` and
``preset`` on an inverted ``QN``. If both or neither answer, that is a
named error, never a fabricated arc.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import liberty_writer
from .characterize_bisect import ConstraintError
from .characterize_cards import slug, spice_number

#: ``timing_sense`` of a forced arc: whether the pin's *asserting* edge and
#: the output edge it causes point the same way.
_SENSES = {True: "positive_unate", False: "negative_unate"}

#: The two primings of each (arc, async pin) pair, and the output edge each
#: one can show: ``"a"`` primes the output high (so only a fall can follow),
#: ``"b"`` primes it low (only a rise).
PRIMINGS = (("a", False), ("b", True))


@dataclass(frozen=True)
class AsyncCase:
    """One (clock-to-output arc, async pin) pair to be probed."""

    output_pin: str
    pin: str
    #: The pin's asserted level, and the level it idles at.
    active_level: bool
    #: Data/side state priming ``output_pin`` high, and the one priming it
    #: low -- both already carry every *other* async pin at its inactive
    #: level (:func:`characterize_sequential.derive_sequential_arcs`).
    high_state: tuple[tuple[str, bool], ...]
    low_state: tuple[tuple[str, bool], ...]

    def state(self, out_rising: bool) -> tuple[tuple[str, bool], ...]:
        """The priming whose output edge is ``out_rising``: to see a rise the
        output must start low, and vice versa."""
        return self.low_state if out_rising else self.high_state


def derive_async_cases(
    cell: dict[str, Any], arcs: tuple[Any, ...]
) -> tuple[AsyncCase, ...]:
    """One case per (clock-to-output arc, declared async pin).

    The two primings come straight off the clock-to-output arc: its
    ``side_inputs`` is the data state under which that output is high and
    ``fall_side_inputs`` the state under which it is low, which is exactly
    what the clock must capture to set the output up for each probe.
    """
    base = {p: False for p in cell["input_pins"] if p != cell["clock_pin"]}
    cases: list[AsyncCase] = []
    for arc in arcs:
        high = {**base, **dict(arc.side_inputs)}
        low = {**base, **dict(arc.fall_side_inputs)}
        for spec in cell["async_pins"]:
            cases.append(
                AsyncCase(
                    output_pin=arc.output_pin,
                    pin=spec.pin,
                    active_level=spec.active_level,
                    high_state=tuple(
                        (n, v) for n, v in sorted(high.items()) if n != spec.pin
                    ),
                    low_state=tuple(
                        (n, v) for n, v in sorted(low.items()) if n != spec.pin
                    ),
                )
            )
    return tuple(cases)


def _schedule(settle: float, max_ramp: float) -> tuple[float, float, float, float]:
    """``(prime, pulse_w, assert_ns, stop_ns)``.

    One priming clock pulse sets the output up; the clock then idles for the
    rest of the run, so the only event after ``assert_ns`` is the async pin's
    own assertion and whatever it forces.
    """
    pulse_w = max_ramp + settle
    prime = settle
    assert_ns = prime + pulse_w + max_ramp + settle
    return prime, pulse_w, assert_ns, assert_ns + max_ramp + settle


def _pwl(name: str, node: str, points: list[tuple[float, float]]) -> str:
    body = " ".join(f"{spice_number(t)}n {spice_number(v)}" for t, v in points)
    return f"{name} {node} 0 PWL({body})"


def build_async_deck(
    *,
    cell: dict[str, Any],
    cases: tuple[AsyncCase, ...],
    probes: list[tuple[int, int, int, bool]],
    rail_nodes: dict[str, str],
    vdd: float,
    thresholds: liberty_writer.Thresholds,
    ramps_ns: tuple[float, ...],
    settle_ns: float,
    loads_pf: tuple[float, ...],
    edge: str,
) -> tuple[str, list[dict[str, Any]], float]:
    """One deck of ``probes`` -- ``(case index, slew index, load index,
    out_rising)`` -- returning ``(netlist_text, measurements, stop_ns)``.

    Each probe is one cell instance: the clock primes the output, then the
    async pin is asserted with the grid row's own ramp. The two ``.meas``
    cards read the pin-to-output delay and the output's transition, both
    gated with ``TD`` past the priming pulse so the priming edge can never
    be mistaken for the forced one.
    """
    max_ramp = max(ramps_ns)
    prime, pulse_w, assert_ns, stop = _schedule(settle_ns, max_ramp)
    idle, active = (0.0, vdd) if edge == "rising" else (vdd, 0.0)
    lines = [
        "* klt characterize -- async set/reset propagation probes, do not edit",
        f".include {cell['netlist_path']}",
        f"Vdd vdd 0 DC {spice_number(vdd)}",
    ]
    measurements: list[dict[str, Any]] = []
    td = f"TD={spice_number(assert_ns)}n"
    for k, (case_index, si, li, out_rising) in enumerate(probes):
        case = cases[case_index]
        ramp = ramps_ns[si]
        load_pf = loads_pf[li]
        tag = f"n{k}"
        asserted = vdd if case.active_level else 0.0
        idle_level = vdd - asserted
        clk_points = [
            (0.0, idle),
            (prime, idle),
            (prime + max_ramp, active),
            (prime + pulse_w, active),
            (prime + pulse_w + max_ramp, idle),
            (stop, idle),
        ]
        pin_points = [
            (0.0, idle_level),
            (assert_ns, idle_level),
            (assert_ns + ramp, asserted),
        ]
        node_of: dict[str, str] = {
            cell["clock_pin"]: f"ck_{tag}",
            case.pin: f"ap_{tag}",
        }
        for name, value in case.state(out_rising):
            node_of[name] = "vdd" if value else "0"
        for name in cell["output_pins"]:
            node_of[name] = f"o_{tag}_{slug(name)}"
        nodes = [node_of.get(t) or rail_nodes[t] for t in cell["terminals"]]
        lines.append(_pwl(f"Vk{tag}", f"ck_{tag}", clk_points))
        lines.append(_pwl(f"Va{tag}", f"ap_{tag}", pin_points))
        lines.append(f"X{tag} {' '.join(nodes)} {cell['subckt']}")
        for name in cell["output_pins"]:
            lines.append(
                f"C{tag}_{slug(name)} {node_of[name]} 0 {spice_number(load_pf)}p"
            )
        out_node = node_of[case.output_pin]
        in_edge = "RISE" if case.active_level else "FALL"
        in_thr = (
            thresholds.input_threshold_pct_rise
            if case.active_level
            else thresholds.input_threshold_pct_fall
        )
        out_edge = "RISE" if out_rising else "FALL"
        out_thr = (
            thresholds.output_threshold_pct_rise
            if out_rising
            else thresholds.output_threshold_pct_fall
        )
        if out_rising:
            lo_thr = thresholds.slew_lower_threshold_pct_rise
            hi_thr = thresholds.slew_upper_threshold_pct_rise
            first, second = lo_thr, hi_thr
        else:
            lo_thr = thresholds.slew_lower_threshold_pct_fall
            hi_thr = thresholds.slew_upper_threshold_pct_fall
            first, second = hi_thr, lo_thr
        measurements.append(
            {
                "name": f"{tag}_d",
                "spice": (
                    f".meas tran {tag}_d TRIG v(ap_{tag}) "
                    f"VAL={spice_number(vdd * in_thr / 100.0)} {in_edge}=1 {td} "
                    f"TARG v({out_node}) "
                    f"VAL={spice_number(vdd * out_thr / 100.0)} {out_edge}=1 {td}"
                ),
                "unit": "s",
            }
        )
        measurements.append(
            {
                "name": f"{tag}_t",
                "spice": (
                    f".meas tran {tag}_t TRIG v({out_node}) "
                    f"VAL={spice_number(vdd * first / 100.0)} {out_edge}=1 {td} "
                    f"TARG v({out_node}) "
                    f"VAL={spice_number(vdd * second / 100.0)} {out_edge}=1 {td}"
                ),
                "unit": "s",
            }
        )
    return "\n".join(lines) + "\n", measurements, stop


def resolve_direction(
    case: AsyncCase,
    raw: dict[str, Any],
    index_of: dict[tuple[int, int, int, bool], int],
    case_index: int,
    slews: int,
    loads: int,
) -> bool:
    """Which way ``case``'s output is forced, read off the two primings.

    Returns ``out_rising``. Raises :class:`ConstraintError` when the two
    primings disagree with "exactly one of them moves": no edge at all means
    the pin does not reach this output, both means the probe is not
    measuring a forced level.
    """
    answered: dict[bool, int] = {}
    for out_rising in (True, False):
        count = 0
        for si in range(slews):
            for li in range(loads):
                k = index_of[(case_index, si, li, out_rising)]
                if raw.get(f"n{k}_d") is not None:
                    count += 1
        answered[out_rising] = count
    total = slews * loads
    complete = [edge for edge, count in answered.items() if count == total]
    partial = [edge for edge, count in answered.items() if 0 < count < total]
    where = f"async pin '{case.pin}' -> output '{case.output_pin}'"
    if partial or len(complete) != 1:
        if not complete and not partial:
            raise ConstraintError(
                f"{where}: asserting the pin moved the output from neither "
                "the output-high nor the output-low priming, so the pin "
                "does not force this output -- remove it from "
                "cell.async_pins, or check the cell's netlist and the "
                "output's next-state function"
            )
        raise ConstraintError(
            f"{where}: asserting the pin did not force one consistent "
            f"direction over the whole grid (output-rise answered "
            f"{answered[True]}/{total} grid points, output-fall "
            f"{answered[False]}/{total}) -- the probe is not measuring a "
            "forced level; check the cell's netlist and request.options"
        )
    return complete[0]


def build_async_arcs(
    *,
    cases: tuple[AsyncCase, ...],
    raw: dict[str, Any],
    probes: list[tuple[int, int, int, bool]],
    grid_ns: tuple[float, ...],
    loads_pf: tuple[float, ...],
    thresholds: liberty_writer.Thresholds,
    clock_pin: str,
) -> list[dict[str, Any]]:
    """One entry per case: the measured direction, its two tables and the
    Liberty group names, ready for :func:`liberty_async` and
    :func:`response_entries`."""
    index_of = {probe[:4]: k for k, probe in enumerate(probes)}
    derate = thresholds.slew_derate_from_library
    entries: list[dict[str, Any]] = []
    for case_index, case in enumerate(cases):
        out_rising = resolve_direction(
            case, raw, index_of, case_index, len(grid_ns), len(loads_pf)
        )
        delay_rows: list[tuple[float, ...]] = []
        transition_rows: list[tuple[float, ...]] = []
        for si in range(len(grid_ns)):
            delays: list[float] = []
            transitions: list[float] = []
            for li in range(len(loads_pf)):
                k = index_of[(case_index, si, li, out_rising)]
                transition = raw.get(f"n{k}_t")
                if transition is None:
                    raise ConstraintError(
                        f"async pin '{case.pin}' -> output "
                        f"'{case.output_pin}': the forced edge at "
                        f"{grid_ns[si]} ns transition / {loads_pf[li]} pF "
                        "load never crossed both slew thresholds, so its "
                        "transition time cannot be reported"
                    )
                delays.append(raw[f"n{k}_d"] * 1e9)
                transitions.append(transition * 1e9 * derate)
            delay_rows.append(tuple(delays))
            transition_rows.append(tuple(transitions))
        entries.append(
            {
                "case": case,
                "out_rising": out_rising,
                "timing_type": "preset" if out_rising else "clear",
                "timing_sense": _SENSES[case.active_level == out_rising],
                "related_pin": case.pin,
                "clock_pin": clock_pin,
                "delay": liberty_writer.Table2D(
                    index_1=grid_ns, index_2=loads_pf, values=tuple(delay_rows)
                ),
                "transition": liberty_writer.Table2D(
                    index_1=grid_ns, index_2=loads_pf, values=tuple(transition_rows)
                ),
            }
        )
    return entries


def liberty_async(
    entries: list[dict[str, Any]],
) -> dict[str, tuple[liberty_writer.TimingArc, ...]]:
    """Group the measured arcs into ``{output pin: (TimingArc, ...)}``.

    A forced arc carries one direction only: the pin drives the output to a
    level and holds it there, so there is no opposite edge to measure and the
    other two tables are deliberately absent rather than fabricated.
    """
    by_output: dict[str, list[liberty_writer.TimingArc]] = {}
    for entry in entries:
        rising = entry["out_rising"]
        by_output.setdefault(entry["case"].output_pin, []).append(
            liberty_writer.TimingArc(
                related_pin=entry["related_pin"],
                timing_sense=entry["timing_sense"],
                timing_type=entry["timing_type"],
                cell_rise=entry["delay"] if rising else None,
                rise_transition=entry["transition"] if rising else None,
                cell_fall=None if rising else entry["delay"],
                fall_transition=None if rising else entry["transition"],
            )
        )
    return {name: tuple(arcs) for name, arcs in by_output.items()}


def response_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The JSON ``async_arcs[]`` list: one object per (async pin, output)."""
    out = []
    for entry in entries:
        case = entry["case"]
        table = {
            "index_1": list(entry["delay"].index_1),
            "index_2": list(entry["delay"].index_2),
        }
        rising = entry["out_rising"]
        delay_name = "cell_rise" if rising else "cell_fall"
        transition_name = "rise_transition" if rising else "fall_transition"
        out.append(
            {
                "pin": case.pin,
                "output_pin": case.output_pin,
                "related_pin": case.pin,
                "timing_type": entry["timing_type"],
                "timing_sense": entry["timing_sense"],
                "forced_level": "high" if rising else "low",
                "active": "high" if case.active_level else "low",
                delay_name: {
                    **table,
                    "values": [list(row) for row in entry["delay"].values],
                },
                transition_name: {
                    **table,
                    "values": [list(row) for row in entry["transition"].values],
                },
                "side_inputs": dict(case.state(rising)),
            }
        )
    return out


def run_async(
    *,
    cell: dict[str, Any],
    arcs: tuple[Any, ...],
    grid_ns: tuple[float, ...],
    loads_pf: tuple[float, ...],
    rail_nodes: dict[str, str],
    vdd: float,
    thresholds: liberty_writer.Thresholds,
    ramps_ns: tuple[float, ...],
    settle_ns: float,
    run_deck: Any,
) -> dict[str, Any]:
    """Measure every declared async pin's propagation arcs in one deck.

    Returns ``{"liberty": {output pin: (TimingArc, ...)}, "entries": [...],
    "measured": [...]}`` -- the Liberty groups to merge onto the output pins,
    the JSON ``async_arcs[]`` list, and the internal per-case records
    (:func:`build_async_arcs`) that
    :func:`characterize_constraints.derive_async_cases` reads the measured
    forced direction from.
    """
    cases = derive_async_cases(cell, arcs)
    if not cases:
        return {"liberty": {}, "entries": [], "measured": []}
    probes = [
        (case_index, si, li, out_rising)
        for case_index in range(len(cases))
        for si in range(len(grid_ns))
        for li in range(len(loads_pf))
        for _, out_rising in PRIMINGS
    ]
    text, measurements, stop = build_async_deck(
        cell=cell,
        cases=cases,
        probes=probes,
        rail_nodes=rail_nodes,
        vdd=vdd,
        thresholds=thresholds,
        ramps_ns=ramps_ns,
        settle_ns=settle_ns,
        loads_pf=loads_pf,
        edge=cell["clock_edge"],
    )
    raw = run_deck(text, measurements, "async", stop)
    entries = build_async_arcs(
        cases=cases,
        raw=raw,
        probes=probes,
        grid_ns=grid_ns,
        loads_pf=loads_pf,
        thresholds=thresholds,
        clock_pin=cell["clock_pin"],
    )
    return {
        "liberty": liberty_async(entries),
        "entries": response_entries(entries),
        "measured": entries,
    }
