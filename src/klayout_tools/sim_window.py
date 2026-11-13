"""Shared window helpers for the windowed ``klt sim`` measurement forms.

``crossings``, ``cycle_mean``, ``energy``, ``eye``, ``settled``,
``step_resolution`` and ``stress`` all bound a reduction to a slice of the
transient: each takes a ``from``/``to`` (or ``sample_at``) time, each has to
reject a bound that is not a time, and the two that integrate over the window
(``energy``, ``stress``) also refuse a window the ``tran`` analysis never
reaches. Those rules are identical across the forms, so they live here instead
of once per form module -- the forms differ in what they *compute* from the
window, not in how they parse or police it.

Every diagnostic raised here names the **caller's own form** (``form=``), so a
bad ``cycle_mean.from`` is reported as a ``cycle_mean`` problem rather than
leaking the module this helper happens to live in.
"""

from __future__ import annotations

import bisect
import math
from typing import Any

from ._jsonnum import is_number
from .sim_errors import SimError

#: Slack, as a fraction of the analysis window, when checking that a requested
#: edge lies inside the saved samples or the declared ``tran`` window (float
#: round-off in the simulator's own final timepoint).
EDGE_TOLERANCE = 1e-9


def window_bound(
    name: Any,
    body: dict[str, Any],
    key: str,
    *,
    form: str,
    field: str | None = None,
) -> float | None:
    """One window bound of ``body[key]`` in seconds: a number, or a SPICE time
    literal such as ``"10n"``. ``None`` when absent.

    ``form`` is the measurement form the bound belongs to (``"energy"``,
    ``"cycle_mean"``, ...); the diagnostic names ``form.key``. Pass ``field``
    when the caller-visible path is not ``key`` itself -- for example
    ``field="level.sample_at"`` for a bound nested under ``crossings.level``.
    """
    from .sim import _parse_spice_seconds

    raw = body.get(key)
    if raw is None:
        return None
    seconds: float | None = None
    if is_number(raw):
        seconds = float(raw)
    elif isinstance(raw, str):
        seconds = _parse_spice_seconds(raw)
    if seconds is None or not math.isfinite(seconds):
        raise SimError(
            f"measurement '{name}': {form}.{field or key} must be a time in "
            f'seconds -- a number or a SPICE time literal such as "10n" '
            f"(got {raw!r})"
        )
    return seconds


def validate_window_inside_tran(
    declared: list[dict[str, Any]], analysis: dict[str, Any], *, form: str
) -> None:
    """Refuse a ``from``/``to`` of any entry in ``declared`` that falls outside
    the ``tran`` analysis window (``tstart``..``tstop``).

    ``declared`` are the entries that declare ``form``, each carrying its body
    under ``spec[form]``. A no-op when ``analysis.args`` does not yield both
    tokens -- an unparseable sweep is diagnosed where it is emitted, not here.
    """
    from .sim import _parse_spice_seconds

    tokens = str(analysis.get("args") or "").split()
    tstop = _parse_spice_seconds(tokens[1]) if len(tokens) > 1 else None
    tstart = _parse_spice_seconds(tokens[2]) if len(tokens) > 2 else 0.0
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
