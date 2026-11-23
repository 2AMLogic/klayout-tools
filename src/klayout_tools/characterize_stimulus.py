"""Clock/data stimulus for sequential cells in ``klt characterize``
(issue #4755): the ``PWL()`` source lines for the three-pulse clock and
the data pins, and :func:`stimulus_schedule` -- the edge-time schedule
both are built from.

Split out of :mod:`klayout_tools.characterize_sequential` (size ratchet,
issue #5520): that module keeps request validation and arc derivation,
while the deck builders (:mod:`klayout_tools.characterize_instances`) and
the leg planner in :mod:`klayout_tools.characterize` consume this half.
"""

from __future__ import annotations

from .characterize_cards import _pwl


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
