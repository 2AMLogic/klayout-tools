"""Shared window helpers for the windowed ``klt sim`` measurement forms.

``crossings``, ``cycle_mean``, ``energy``, ``eye``, ``settled``,
``step_resolution`` and ``stress`` all bound a reduction to a slice of the
transient: each takes a ``from``/``to`` (or ``sample_at``) time, each has to
reject a bound that is not a time, and the two that integrate over the window
(``energy``, ``stress``) also refuse a window the ``tran`` analysis never
reaches -- at validation, against the declared sweep, and again at
reduction, against the samples the simulator actually saved. Those rules are
identical across the forms, so they live here instead of once per form
module -- the forms differ in what they *compute* from the window, not in how
they parse or police it.

Every diagnostic raised here names the **caller's own form** (``form=``), so a
bad ``cycle_mean.from`` is reported as a ``cycle_mean`` problem rather than
leaking the module this helper happens to live in.
"""

from __future__ import annotations

import bisect
import math
from typing import Any

from ._jsonnum import is_finite_number
from ._spice_number import parse as parse_spice_number
from .sim_errors import SimError

#: Slack, as a fraction of the analysis window, when checking that a requested
#: edge lies inside the saved samples or the declared ``tran`` window (float
#: round-off in the simulator's own final timepoint).
EDGE_TOLERANCE = 1e-9


#: The unit wording of the :func:`window_bound` diagnostic, keyed by its
#: ``axis=`` argument (see that function's docstring for when each applies).
_AXIS_REQUIREMENTS = {
    "time": 'a time in seconds -- a number or a SPICE time literal such as "10n"',
    "sweep": (
        'a number or a SPICE literal such as "10n" in the sweep\'s own unit '
        "-- the swept source's value (volts/amps), not seconds"
    ),
    "axis": (
        'a number or a SPICE literal such as "10n" -- simulated seconds on '
        "tran, the swept source's unit on dc"
    ),
}


def window_bound(
    name: Any,
    body: dict[str, Any],
    key: str,
    *,
    form: str,
    field: str | None = None,
    axis: str = "time",
) -> float | None:
    """One window bound of ``body[key]`` in seconds: a number, or a SPICE time
    literal such as ``"10n"``. ``None`` when absent.

    ``form`` is the measurement form the bound belongs to (``"energy"``,
    ``"cycle_mean"``, ...); the diagnostic names ``form.key``. Pass ``field``
    when the caller-visible path is not ``key`` itself -- for example
    ``field="level.sample_at"`` for a bound nested under ``crossings.level``.

    ``axis`` words the unit the diagnostic demands: ``"time"`` (the default)
    is seconds -- a tran time axis; ``"sweep"`` is the swept source's own
    unit (volts/amps), for a bound read off a ``dc`` sweep; ``"axis"`` stays
    neutral, for a form that runs on either (``crossings``) and is
    shape-checked before the analysis kind is known. The parsing is the same
    on every axis -- SPICE suffixes are multipliers, so ``"1m"`` is 0.001:
    1 mV on a voltage sweep, 1 ms on time.
    """
    raw = body.get(key)
    if raw is None:
        return None
    seconds: float | None = None
    if is_finite_number(raw):
        seconds = float(raw)
    elif isinstance(raw, str):
        seconds = parse_spice_number(raw)
    if seconds is None or not math.isfinite(seconds):
        raise SimError(
            f"measurement '{name}': {form}.{field or key} must be "
            f"{_AXIS_REQUIREMENTS[axis]} (got {raw!r})"
        )
    return seconds


def parse_tran_window(
    analysis: dict[str, Any],
) -> tuple[str, str, float, float] | None:
    """Return the original step and stop tokens and their durations in seconds.

    Eye validation and simulation preflight use the first two positive time
    values of a transient analysis. Return None for other analysis kinds or
    an invalid step or stop.
    """
    if analysis.get("kind") != "tran":
        return None
    tokens = (analysis.get("args") or "").split()
    if len(tokens) < 2:
        return None
    step_s = parse_spice_number(tokens[0])
    window_s = parse_spice_number(tokens[1])
    if step_s is None or window_s is None or step_s <= 0 or window_s <= 0:
        return None
    return tokens[0], tokens[1], step_s, window_s


def validate_window_inside_tran(
    declared: list[dict[str, Any]], analysis: dict[str, Any], *, form: str
) -> None:
    """Refuse a ``from``/``to`` of any entry in ``declared`` that falls outside
    the ``tran`` analysis window (``tstart``..``tstop``).

    ``declared`` are the entries that declare ``form``, each carrying its body
    under ``spec[form]``. A no-op when ``analysis.args`` does not yield both
    tokens -- an unparseable sweep is diagnosed where it is emitted, not here.
    """
    tokens = str(analysis.get("args") or "").split()
    tstop = parse_spice_number(tokens[1]) if len(tokens) > 1 else None
    tstart = parse_spice_number(tokens[2]) if len(tokens) > 2 else 0.0
    if tstop is None or tstart is None:
        return
    for spec in declared:
        for key in ("from", "to"):
            bound = window_bound(spec["name"], spec[form], key, form=form)
            if bound is not None and not (
                tstart - EDGE_TOLERANCE * tstop <= bound <= tstop * (1 + EDGE_TOLERANCE)
            ):
                raise SimError(
                    f"measurement '{spec['name']}': {form}.{key} ({bound!r} s)"
                    f" is outside the transient analysis window "
                    f"[{tstart!r}, {tstop!r}] s (analysis.args "
                    f"{analysis.get('args')!r})"
                )


def value_at(scale: list[float], values: list[float], t: float) -> float:
    """Linear interpolation of the sampled signal at ``t`` (inside the scale).

    Clamps to the first/last sample outside it, so a window edge that round-off
    pushes a hair past the final timepoint still yields that sample.
    """
    index = bisect.bisect_left(scale, t)
    if index <= 0:
        return values[0]
    if index >= len(scale):
        return values[-1]
    t0, t1 = scale[index - 1], scale[index]
    if t1 == t0:
        return values[index]
    return values[index - 1] + (values[index] - values[index - 1]) * (
        (t - t0) / (t1 - t0)
    )


def resolve_sample_window(
    scale: list[float],
    count: int,
    *,
    start: float | None = None,
    stop: float | None = None,
) -> tuple[float, float, str | None]:
    """Resolve the ``[start, stop]`` window of a reduction over the first
    ``count`` samples of ``scale`` to ``(lo, hi, error)``.

    ``count`` is how many of ``scale``'s samples the caller can use (the
    shortest of its vectors); the default window is the whole of them.
    ``error`` is ``None`` when the window is usable and ``lo``/``hi`` are the
    edges to reduce over; otherwise it is the per-measurement reason (fewer
    than two samples, a window the saved samples do not reach within
    :data:`EDGE_TOLERANCE`, or one that holds no time) and both edges are
    ``0.0``.
    """
    if count < 2:
        return 0.0, 0.0, "fewer than two samples"
    lo = scale[0] if start is None else start
    hi = scale[count - 1] if stop is None else stop
    slack = EDGE_TOLERANCE * max(abs(scale[count - 1] - scale[0]), abs(hi), 1e-30)
    if lo < scale[0] - slack or hi > scale[count - 1] + slack:
        outside = (
            f"window [{lo!r}, {hi!r}] s is outside the saved samples "
            f"[{scale[0]!r}, {scale[count - 1]!r}] s"
        )
        return 0.0, 0.0, outside
    if not hi > lo:
        return 0.0, 0.0, f"window [{lo!r}, {hi!r}] s is empty"
    return lo, hi, None
