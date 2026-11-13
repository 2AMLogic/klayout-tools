"""Sequential-cell (clock-to-output) request validation, arc derivation and
clock/data stimulus for ``klt characterize`` (issue #4755), plus the
``cell.async_pins`` declaration of asynchronous set/reset pins (issue #5520)
and the level-sensitive **latch** declaration layered on top of it
(issue #5521).

Split out of :mod:`klayout_tools.characterize` to keep that module from
growing. Errors are raised as :class:`SequentialSpecError`; the caller
re-raises them as its own ``CharacterizeError``.

A latch is declared with ``cell.enable_pin`` / ``cell.enable_level`` rather
than ``cell.clock_pin``, and :func:`resolve_sequential` resolves it *into*
the existing ``clock_pin``/``clock_edge`` slots -- the enable becomes the
clock pin and the clock edge becomes the enable's **opening** edge -- so the
three-pulse stimulus, the capacitance probes and the deck builder need no
latch special case. The latch flag it returns alongside is what adds the
transparent data-to-output arc (:func:`derive_latch_arcs`) and moves
setup/hold onto the **closing** edge (:func:`closing_edge`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .characterize_arcs import (
    DerivedArc,
    FunctionError,
    derive_arcs,
    derive_sequential_arc,
    parse_function,
)
from .characterize_cards import spice_number

#: Accepted values of ``cell.enable_level`` (a latch enable's active level).
ENABLE_LEVELS = ("high", "low")


class SequentialSpecError(Exception):
    """A request's sequential-cell fields are invalid."""


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


@dataclass(frozen=True)
class AsyncPin:
    """One asynchronous set/reset pin of a sequential cell (issue #5520).

    ``kind`` is what the pin does to the cell's stored state (``"clear"`` ->
    0, ``"preset"`` -> 1) and ``active`` is the level at which it does it.
    Neither says which way a given *output* moves -- an inverted output moves
    the other way -- so the emitted arc's ``timing_type`` is decided by the
    measured output edge, not by ``kind``; see
    :mod:`klayout_tools.characterize_async`.
    """

    pin: str
    kind: str  # "clear" / "preset"
    active: str  # "low" / "high"

    @property
    def active_level(self) -> bool:
        """The logic level that asserts the pin."""
        return self.active == "high"

    @property
    def inactive_level(self) -> bool:
        """The level the pin is held at for every other measurement."""
        return not self.active_level


_ASYNC_KINDS = ("clear", "preset")
_ASYNC_ACTIVE = ("low", "high")


def _resolve_async_pin(
    entry: Any,
    at: str,
    where: str,
    inputs: set[str],
    clock_pin: str,
    static_names: set[str],
    constrained_pins: tuple[str, ...],
) -> AsyncPin:
    """Validate one ``async_pins[]`` entry."""
    if not isinstance(entry, dict):
        raise SequentialSpecError(f"{at} must be an object")
    name = entry.get("pin")
    if not isinstance(name, str) or name not in inputs or name == clock_pin:
        raise SequentialSpecError(
            f"{at}.pin names {name!r}, which is not a declared non-clock input pin"
        )
    if name in static_names:
        raise SequentialSpecError(
            f"{at}.pin names {name!r}, which {where}.static_inputs also ties "
            "off -- an async pin is driven by the generated stimulus and "
            "held inactive for every other measurement, so it must not be "
            "declared static as well"
        )
    if name in constrained_pins:
        raise SequentialSpecError(
            f"{at}.pin names {name!r}, which {where}.constrained_pins also "
            "lists -- setup/hold is for data pins; an async pin gets "
            "recovery/removal instead"
        )
    kind = entry.get("kind")
    if kind not in _ASYNC_KINDS:
        raise SequentialSpecError(
            f'{at}.kind must be "clear" or "preset" (got {kind!r})'
        )
    active = entry.get("active")
    if active not in _ASYNC_ACTIVE:
        raise SequentialSpecError(
            f'{at}.active must be "low" or "high" (got {active!r})'
        )
    return AsyncPin(pin=name, kind=kind, active=active)


def resolve_async_pins(
    spec: dict[str, Any],
    pins: tuple[dict[str, Any], ...],
    clock_pin: str | None,
    statics: tuple[tuple[str, bool], ...],
    constrained_pins: tuple[str, ...],
    where: str,
) -> tuple[AsyncPin, ...]:
    """Validate ``cell.async_pins`` into :class:`AsyncPin` records.

    An async pin is a declared non-clock input that is driven by this
    command's own stimulus, so every other way of pinning it down is a
    contradiction rather than a redundancy: ``static_inputs`` would tie it
    off, ``constrained_pins`` would bisect it as data, and an output
    ``function`` naming it would make it part of the next state. Each is a
    named error (the ``function`` clash is caught in
    :func:`derive_sequential_arcs`, which is where the functions are parsed).
    """
    raw = spec.get("async_pins")
    if raw is None:
        return ()
    if clock_pin is None:
        raise SequentialSpecError(f"{where}.async_pins requires {where}.clock_pin")
    if not isinstance(raw, list) or not raw:
        raise SequentialSpecError(
            f"{where}.async_pins must be a non-empty array of "
            '{"pin", "kind", "active"} objects'
        )
    inputs = {pin["name"] for pin in pins if pin["direction"] == "input"}
    static_names = {name for name, _ in statics}
    resolved: list[AsyncPin] = []
    for index, entry in enumerate(raw):
        pin_spec = _resolve_async_pin(
            entry,
            f"{where}.async_pins[{index}]",
            where,
            inputs,
            clock_pin,
            static_names,
            constrained_pins,
        )
        if any(pin_spec.pin == seen.pin for seen in resolved):
            raise SequentialSpecError(
                f"{where}.async_pins lists {pin_spec.pin!r} twice"
            )
        resolved.append(pin_spec)
    return tuple(resolved)


def resolve_sequential(
    spec: dict[str, Any], pins: tuple[dict[str, Any], ...], where: str
) -> tuple[str | None, str, tuple[tuple[str, bool], ...], bool]:
    """Validate the sequential declaration: ``clock_pin`` / ``clock_edge`` /
    ``static_inputs`` (issue #4755), or ``enable_pin`` / ``enable_level``
    (issue #5521).

    Returns ``(clock_pin, clock_edge, static_inputs, latch)``. Declaring one
    of ``clock_pin`` / ``enable_pin`` is the only thing that makes a cell
    sequential; without either, every output ``function`` is parsed as a
    combinational expression exactly as before.

    For a latch, ``clock_pin`` is the enable and ``clock_edge`` is its
    **opening** edge -- ``"rising"`` for an active-high enable -- which is
    what the rest of the pipeline consumes.
    """
    clock_pin = spec.get("clock_pin")
    enable_pin = spec.get("enable_pin")
    if clock_pin is not None and enable_pin is not None:
        raise SequentialSpecError(
            f"{where}.clock_pin and {where}.enable_pin are mutually exclusive: "
            "clock_pin asserts edge-triggered semantics (one clock-to-output "
            "arc on the active edge, setup/hold against that edge), "
            "enable_pin level-sensitive latch semantics (a transparent "
            "data-to-output arc, an enable-to-output arc on the opening edge, "
            "and setup/hold against the closing edge)"
        )
    if enable_pin is not None:
        return _resolve_latch(spec, pins, enable_pin, where)
    if "enable_level" in spec:
        raise SequentialSpecError(f"{where}.enable_level requires {where}.enable_pin")
    edge = spec.get("clock_edge", "rising")
    statics = spec.get("static_inputs")
    if clock_pin is None:
        for key in ("clock_edge", "static_inputs", "async_pins"):
            if key in spec:
                raise SequentialSpecError(f"{where}.{key} requires {where}.clock_pin")
        return None, "rising", (), False
    inputs = [pin["name"] for pin in pins if pin["direction"] == "input"]
    if clock_pin not in inputs:
        raise SequentialSpecError(
            f"{where}.clock_pin must name a declared input pin (got {clock_pin!r})"
        )
    if edge not in ("rising", "falling"):
        raise SequentialSpecError(
            f'{where}.clock_edge must be "rising" or "falling" (got {edge!r})'
        )
    return clock_pin, edge, _resolve_statics(statics, inputs, clock_pin, where), False


def _resolve_latch(
    spec: dict[str, Any],
    pins: tuple[dict[str, Any], ...],
    enable_pin: Any,
    where: str,
) -> tuple[str, str, tuple[tuple[str, bool], ...], bool]:
    """Validate ``enable_pin`` / ``enable_level`` into the ``clock_pin`` /
    ``clock_edge`` slots, with the edge being the enable's opening edge."""
    if "clock_edge" in spec:
        raise SequentialSpecError(
            f"{where}.clock_edge does not apply to a latch: a latch is declared "
            f"by {where}.enable_level, and both of the enable's edges follow "
            "from it (opening = the edge onto the active level, closing = the "
            "edge away from it)"
        )
    inputs = [pin["name"] for pin in pins if pin["direction"] == "input"]
    if enable_pin not in inputs:
        raise SequentialSpecError(
            f"{where}.enable_pin must name a declared input pin (got {enable_pin!r})"
        )
    level = spec.get("enable_level", "high")
    if level not in ENABLE_LEVELS:
        raise SequentialSpecError(
            f'{where}.enable_level must be "high" or "low" (got {level!r})'
        )
    statics = _resolve_statics(spec.get("static_inputs"), inputs, enable_pin, where)
    return enable_pin, ("rising" if level == "high" else "falling"), statics, True


def closing_edge(clock_edge: str) -> str:
    """The latch edge setup/hold are referenced to: the one that takes the
    enable *away* from its active level.

    ``clock_edge`` holds the enable's **opening** edge for a latch (see
    :func:`resolve_sequential`), so the closing edge is simply its opposite:
    an active-high enable opens rising and closes falling, hence
    ``setup_falling``/``hold_falling``.
    """
    return "falling" if clock_edge == "rising" else "rising"


def async_inactive_state(cell: dict[str, Any]) -> tuple[tuple[str, bool], ...]:
    """Every declared async pin at its inactive level (issue #5520).

    Appended to both halves of every clock-to-output arc's side-input state,
    so the existing clock-to-output and setup/hold measurements hold the pin
    inactive *without* the request also listing it in ``static_inputs``.
    """
    return tuple((spec.pin, spec.inactive_level) for spec in cell.get("async_pins", ()))


def derive_sequential_arcs(cell: dict[str, Any]) -> tuple[DerivedArc, ...]:
    """One clock-to-output arc per output pin of a cell declaring
    ``clock_pin``; each output's ``function`` is its next-state expression
    over the data pins.

    Declared async pins are not data pins: they are excluded from the
    next-state cube and held inactive for the whole measurement.
    """
    async_names = {spec.pin for spec in cell.get("async_pins", ())}
    data_pins = tuple(
        p for p in cell["input_pins"] if p != cell["clock_pin"] and p not in async_names
    )
    statics = cell["static_inputs"] + async_inactive_state(cell)
    non_clock = frozenset(p for p in cell["input_pins"] if p != cell["clock_pin"])
    arcs: list[DerivedArc] = []
    for pin in cell["pins"]:
        if pin["direction"] != "output":
            continue
        if async_names:
            _reject_async_in_function(cell, pin, async_names, non_clock)
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


def _reject_async_in_function(
    cell: dict[str, Any],
    pin: dict[str, Any],
    async_names: set[str],
    non_clock: frozenset[str],
) -> None:
    """Name the clash when an output's next-state function references an
    async pin, rather than letting it surface as "not a declared input pin".

    Parsed over *every* non-clock input so the async names are in scope; a
    function that fails to parse for some other reason is left to
    :func:`derive_sequential_arcs`'s own error path.
    """
    try:
        expr = parse_function(pin["function"], allowed_pins=non_clock)
    except FunctionError:
        return
    clash = sorted(name for name in async_names if name in expr.pins)
    if clash:
        raise SequentialSpecError(
            f"cell '{cell['name']}' pin '{pin['name']}': async_pins names "
            f"{', '.join(clash)}, which its next-state function also "
            "references -- an async pin forces the output directly, so it is "
            "not part of the next state"
        )


def derive_latch_arcs(cell: dict[str, Any]) -> tuple[DerivedArc, ...]:
    """Every arc of a level-sensitive latch (issue #5521).

    Two kinds, in this order:

    1. The **enable->output** arc per output pin, exactly what
       :func:`derive_sequential_arcs` already derives -- the enable is the
       resolved ``clock_pin`` and the resolved ``clock_edge`` is its opening
       edge, so the emitted ``timing_type`` is ``rising_edge`` for an
       active-high enable and ``falling_edge`` for an active-low one.
    2. The **transparent data->output** arcs: the output's ``function`` is
       its next-state expression over the data pins, which for a latch is
       also what the output tracks while the enable is open, so the ordinary
       combinational derivation over the *data* pins gives them. Each is
       measured with the enable pinned at its active level -- one more
       side input, which is the mechanism ``static_inputs`` and the
       combinational path already use -- and keeps
       ``timing_type : combinational``.

    A data pin the function does not reference yields no transparent arc, the
    same way an unreferenced input yields no combinational arc.
    """
    arcs = list(derive_sequential_arcs(cell))
    enable_pin = cell["clock_pin"]
    # `clock_edge` is the opening edge, so a rising opening edge is an
    # active-high enable.
    held = ((enable_pin, cell["clock_edge"] == "rising"),) + cell["static_inputs"]
    data_pins = tuple(p for p in cell["input_pins"] if p != enable_pin)
    for pin in cell["pins"]:
        if pin["direction"] != "output":
            continue
        # `derive_sequential_arcs` above already parsed this same expression
        # over these same pins, so a `FunctionError` here is unreachable.
        for arc in derive_arcs(
            output_pin=pin["name"],
            function=pin["function"],
            input_pins=data_pins,
        ):
            arcs.append(replace(arc, side_inputs=arc.side_inputs + held))
    return tuple(arcs)


def _pwl(name: str, node: str, points: list[tuple[float, float]]) -> str:
    body = " ".join(f"{spice_number(t)}n {spice_number(v)}" for t, v in points)
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
