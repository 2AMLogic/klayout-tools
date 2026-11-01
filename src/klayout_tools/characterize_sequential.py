"""Sequential-cell (clock-to-output) request validation, arc derivation and
clock/data stimulus for ``klt characterize`` (issue #4755).

Split out of :mod:`klayout_tools.characterize` to keep that module from
growing. Errors are raised as :class:`SequentialSpecError`; the caller
re-raises them as its own ``CharacterizeError``.
"""

from __future__ import annotations

from typing import Any

from . import liberty_writer
from .characterize_arcs import DerivedArc, FunctionError, derive_sequential_arc


class SequentialSpecError(Exception):
    """A request's sequential-cell fields are invalid."""


def _spice_number(value: float) -> str:
    return liberty_writer.format_number(value, precision=10)


def _resolve_statics(
    statics: Any, inputs: list[str], clock_pin: str, where: str
) -> tuple[tuple[str, bool], ...]:
    """Validate ``static_inputs`` into sorted ``(pin, bool)`` pairs."""
    if statics is None:
        return ()
    if not isinstance(statics, dict):
        raise SequentialSpecError(
            f"{where}.static_inputs must be an object mapping pin name -> boolean"
        )
    resolved: list[tuple[str, bool]] = []
    for name in sorted(statics):
        if name not in inputs or name == clock_pin:
            raise SequentialSpecError(
                f"{where}.static_inputs names {name!r}, which is not a "
                "declared non-clock input pin"
            )
        if not isinstance(statics[name], bool):
            raise SequentialSpecError(f"{where}.static_inputs.{name} must be a boolean")
        resolved.append((name, statics[name]))
    return tuple(resolved)


def resolve_sequential(
    spec: dict[str, Any], pins: tuple[dict[str, Any], ...], where: str
) -> tuple[str | None, str, tuple[tuple[str, bool], ...]]:
    """Validate ``clock_pin`` / ``clock_edge`` / ``static_inputs`` (issue #4755).

    Declaring ``clock_pin`` is the only thing that makes a cell sequential;
    without it every output ``function`` is parsed as a combinational
    expression exactly as before.
    """
    clock_pin = spec.get("clock_pin")
    edge = spec.get("clock_edge", "rising")
    statics = spec.get("static_inputs")
    if clock_pin is None:
        for key in ("clock_edge", "static_inputs"):
            if key in spec:
                raise SequentialSpecError(f"{where}.{key} requires {where}.clock_pin")
        return None, "rising", ()
    inputs = [pin["name"] for pin in pins if pin["direction"] == "input"]
    if clock_pin not in inputs:
        raise SequentialSpecError(
            f"{where}.clock_pin must name a declared input pin (got {clock_pin!r})"
        )
    if edge not in ("rising", "falling"):
        raise SequentialSpecError(
            f'{where}.clock_edge must be "rising" or "falling" (got {edge!r})'
        )
    return clock_pin, edge, _resolve_statics(statics, inputs, clock_pin, where)


def derive_sequential_arcs(cell: dict[str, Any]) -> tuple[DerivedArc, ...]:
    """One clock-to-output arc per output pin of a cell declaring
    ``clock_pin``; each output's ``function`` is its next-state expression
    over the data pins."""
    data_pins = tuple(p for p in cell["input_pins"] if p != cell["clock_pin"])
    statics = cell["static_inputs"]
    arcs: list[DerivedArc] = []
    for pin in cell["pins"]:
        if pin["direction"] != "output":
            continue
        try:
            arc = derive_sequential_arc(
                output_pin=pin["name"],
                next_state=pin["function"],
                data_pins=data_pins,
                clock_pin=cell["clock_pin"],
                clock_edge=cell["clock_edge"],
            )
        except FunctionError as exc:
            raise SequentialSpecError(
                f"cell '{cell['name']}' pin '{pin['name']}' (sequential, "
                f"clock_pin '{cell['clock_pin']}'): {exc}; for a sequential "
                "cell `function` is the next-state expression over the data "
                "input pins"
            ) from exc
        referenced = {name for name, _ in arc.side_inputs}
        clash = [name for name, _ in statics if name in referenced]
        if clash:
            raise SequentialSpecError(
                f"cell '{cell['name']}' pin '{pin['name']}': static_inputs "
                f"names {', '.join(clash)}, which its function also references"
            )
        arcs.append(
            DerivedArc(
                output_pin=arc.output_pin,
                related_pin=arc.related_pin,
                timing_sense=arc.timing_sense,
                measured_sense=arc.measured_sense,
                side_inputs=arc.side_inputs + statics,
                timing_type=arc.timing_type,
                fall_side_inputs=arc.fall_side_inputs + statics,
            )
        )
    return tuple(arcs)


def _pwl(name: str, node: str, points: list[tuple[float, float]]) -> str:
    body = " ".join(f"{_spice_number(t)}n {_spice_number(v)}" for t, v in points)
    return f"{name} {node} 0 PWL({body})"


def clock_pwl_line(
    name: str,
    node: str,
    *,
    vdd: float,
    ramp_ns: float,
    times: dict[str, float],
    edge: str,
) -> str:
    """Priming + two measured clock pulses. For a falling-edge clock the
    pulses are active-low: the clock idles high and dips low."""
    idle, active = (0.0, vdd) if edge == "rising" else (vdd, 0.0)
    points: list[tuple[float, float]] = [(0.0, idle)]
    for key in ("prime", "rise", "fall"):
        start = times[key]
        points += [
            (start, idle),
            (start + ramp_ns, active),
            (start + times["width"], active),
            (start + times["width"] + ramp_ns, idle),
        ]
    return _pwl(name, node, points)


def data_pwl_line(
    name: str,
    node: str,
    *,
    low: bool,
    high: bool,
    vdd: float,
    ramp_ns: float,
    times: dict[str, float],
) -> str:
    """A data pin: the output-low value, the output-high value while the
    clock is low between the priming and first measured pulse, then back."""
    lo_v, hi_v = (vdd if low else 0.0), (vdd if high else 0.0)
    points = [
        (0.0, lo_v),
        (times["data_hi"], lo_v),
        (times["data_hi"] + ramp_ns, hi_v),
        (times["data_lo"], hi_v),
        (times["data_lo"] + ramp_ns, lo_v),
    ]
    return _pwl(name, node, points)


def stimulus_schedule(
    sequential: bool, settle: float, max_ramp: float
) -> tuple[float, float, float, dict[str, float] | None, float]:
    """Edge times ``(rise_start, fall_start, stop_ns, clock_times, pulse_w)``.

    Sequential cells get three clock pulses: a priming pulse (output captures
    its low state), then the two measured pulses -- data moves to the
    output-high state while the clock is low, the next active edge makes the
    output rise, data returns to the low state, and the last active edge makes
    it fall. ``rise_start``/``fall_start`` are the two measured active edges,
    so cap probes and windows keep working.
    """
    pulse_w = max_ramp + settle
    if not sequential:
        rise_start = settle
        fall_start = rise_start + max_ramp + settle
        return rise_start, fall_start, fall_start + max_ramp + settle, None, pulse_w
    prime_start = settle
    data_hi_start = prime_start + pulse_w + max_ramp
    rise_start = data_hi_start + max_ramp + settle
    data_lo_start = rise_start + pulse_w + max_ramp
    fall_start = data_lo_start + max_ramp + settle
    clock_times = {
        "prime": prime_start,
        "data_hi": data_hi_start,
        "rise": rise_start,
        "data_lo": data_lo_start,
        "fall": fall_start,
        "width": pulse_w,
    }
    return rise_start, fall_start, fall_start + pulse_w, clock_times, pulse_w
