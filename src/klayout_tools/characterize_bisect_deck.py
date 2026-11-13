"""Probe-deck generation for ``klt characterize``'s constraint arcs -- the
shared case/error types and the one deck that carries every probe of a
bisection iteration.

Split out of :mod:`klayout_tools.characterize_bisect` to keep that module
under its file-size ratchet; the lockstep search that drives these decks,
and the prose describing what each constraint kind means, live there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import liberty_writer
from .characterize_cards import spice_number

#: Runs one probe deck: ``(netlist_text, measurements, label, stop_ns) ->
#: {measurement name: value in seconds, or None when ngspice found no
#: crossing}``.
DeckRunner = Callable[[str, list[dict[str, Any]], str, float], dict[str, Any]]


class ConstraintError(Exception):
    """Invalid constraint request, or a bisection that cannot be trusted."""


@dataclass(frozen=True)
class ConstraintCase:
    """One (constrained pin, edge, kind) combination -- a data pin's
    setup/hold, or an async pin's recovery/removal."""

    pin: str
    #: ``"rise"`` / ``"fall"``: the constrained pin's transition -- for
    #: recovery/removal, the async pin's *release* edge.
    edge: str
    kind: str  # "setup" / "hold" / "recovery" / "removal"
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


#: Kinds whose constrained edge sits *before* the clock edge (the offset is
#: subtracted); the rest place it after.
_BEFORE_CLOCK = ("setup", "recovery")

#: Kinds measured on an asynchronous set/reset pin rather than a data pin
#: (issue #5520). Their stimulus is a single release edge -- the pin is
#: asserted from t=0 -- so they share the "setup" waveform shape whichever
#: side of the clock edge they land on.
ASYNC_KINDS = ("recovery", "removal")


def _pwl(name: str, node: str, points: list[tuple[float, float]]) -> str:
    body = " ".join(f"{spice_number(t)}n {spice_number(v)}" for t, v in points)
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
    latch: bool = False,
) -> tuple[str, list[dict[str, Any]], float]:
    """One deck of ``probes`` -- ``(case index, clock slew index, data slew
    index, offset_ns)`` -- and the ``.meas`` card(s) that read each one.
    Returns ``(netlist_text, measurements, stop_ns)``.

    ``edge`` is the related pin's *active* edge for a flop and its *opening*
    edge for a latch; a latch's probes are placed relative to the closing
    edge, which is its opposite. A flop probe gets one card (``c<k>_d``, its
    clock-to-output delay); a latch probe gets two (``c<k>_d``, its
    transparent data-to-output delay, and ``c<k>_h``, the output level held
    after the enable closed).
    """
    max_ramp = max(ramps_ns)
    prime, pulse_w, t_early, t_clk, stop = _schedule(settle_ns, max_ramp, window_ns)
    idle, active = (0.0, vdd) if edge == "rising" else (vdd, 0.0)
    clk_rising = edge == "rising"
    # The edge the data is referenced to: the active edge of a flop's clock,
    # the *closing* edge of a latch's enable (so its ramp runs the other way).
    ref_rising = clk_rising != latch
    # Where the held-level card is read: after the close, after the last data
    # edge, and inside the transient window.
    t_final = stop - 0.5 * settle_ns
    lines = [
        "* klt characterize -- constraint probes, do not edit",
        f".include {cell['netlist_path']}",
        f"Vdd vdd 0 DC {spice_number(vdd)}",
    ]
    measurements: list[dict[str, Any]] = []
    for k, (case_index, ci, di, offset) in enumerate(probes):
        case = cases[case_index]
        rc, rd = ramps_ns[ci], ramps_ns[di]
        clk50 = t_clk + rc * _frac(ref_rising, thresholds)
        d_rising = case.edge == "rise"
        new_level = vdd if d_rising else 0.0
        old_level = vdd - new_level
        before = case.kind in _BEFORE_CLOCK
        d50 = clk50 + (-offset if before else offset)
        t_start = d50 - rd * _frac(d_rising, thresholds)
        if latch:
            # One transparency window: the enable opens at `prime` and stays
            # active until the measured *closing* edge at `t_clk`, after
            # which the cell must hold whatever it captured.
            clk_points = [
                (0.0, idle),
                (prime, idle),
                (prime + rc, active),
                (t_clk, active),
                (t_clk + rc, idle),
                (stop, idle),
            ]
        else:
            clk_points = [(0.0, idle)]
            for start in (prime, t_clk):
                clk_points += [(start, idle), (start + rc, active)]
                if start == prime:
                    clk_points += [
                        (start + pulse_w, active),
                        (start + pulse_w + rc, idle),
                    ]
                else:
                    # The measured edge is the last event: the clock stays
                    # active to the end of the run. A trailing falling edge
                    # lands on a PWL breakpoint that ngspice 42 cannot step to
                    # near a metastable bisection probe (timestep too small).
                    clk_points.append((stop, active))
        if case.kind != "hold":
            # setup, and both async kinds: one edge, from the level held
            # since t=0 (the captured value for setup, the asserted level
            # for recovery/removal) to the new one.
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
            lines.append(f"C{tag}_{name} {node_of[name]} 0 {spice_number(load_pf)}p")
        out_thr = (
            thresholds.output_threshold_pct_rise
            if case.out_rising
            else thresholds.output_threshold_pct_fall
        )
        out_node = node_of[case.output_pin]
        out_val = spice_number(vdd * out_thr / 100.0)
        out_dir = "RISE" if case.out_rising else "FALL"
        if latch:
            # The capturing data edge is the one *onto* `captured_level`: the
            # single edge of a setup probe, the early edge of a hold probe.
            # It is the first data edge in its own direction either way, and
            # the only output movement before the one it causes is the settle
            # onto the pre-capture level, which runs the other way -- so
            # neither card needs a TD.
            d_thr = (
                thresholds.input_threshold_pct_rise
                if case.captured_level
                else thresholds.input_threshold_pct_fall
            )
            measurements.append(
                {
                    "name": f"{tag}_d",
                    "spice": (
                        f".meas tran {tag}_d TRIG v(dd_{tag}) "
                        f"VAL={spice_number(vdd * d_thr / 100.0)} "
                        f"{'RISE' if case.captured_level else 'FALL'}=1 "
                        f"TARG v({out_node}) VAL={out_val} {out_dir}=1"
                    ),
                    "unit": "s",
                }
            )
            measurements.append(
                {
                    "name": f"{tag}_h",
                    "spice": (
                        f".meas tran {tag}_h FIND v({out_node}) "
                        f"AT={spice_number(t_final)}n"
                    ),
                    "unit": "V",
                }
            )
            continue
        clk_thr = (
            thresholds.input_threshold_pct_rise
            if clk_rising
            else thresholds.input_threshold_pct_fall
        )
        td = f"TD={spice_number(t_clk)}n"
        measurements.append(
            {
                "name": f"{tag}_d",
                "spice": (
                    f".meas tran {tag}_d TRIG v(ck_{tag}) "
                    f"VAL={spice_number(vdd * clk_thr / 100.0)} "
                    f"{'RISE' if clk_rising else 'FALL'}=1 {td} "
                    f"TARG v({out_node}) VAL={out_val} {out_dir}=1 {td}"
                ),
                "unit": "s",
            }
        )
    return "\n".join(lines) + "\n", measurements, stop
