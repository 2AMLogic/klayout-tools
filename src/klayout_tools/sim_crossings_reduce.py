"""Period / cycle-to-cycle / TIE reductions over a crossing-time series (#56).

Pure functions over the interpolated ``times`` that
:mod:`klayout_tools.sim_crossings` already extracts; requested through the
optional ``crossings.reduce`` list and returned as ``crossings.reductions``.

Definitions (stated once; ``docs/cli/sim.md`` mirrors them), with ``t`` the
returned crossing times and ``k`` = 1 for ``edge`` ``rise``/``fall`` or 2 for
``either`` (one full cycle spans two edges):

- ``period[i] = t[i+k] - t[i]``; reported as ``n``, ``mean``, ``std``
  (**population** standard deviation, divisor ``n``), ``min``, ``max``.
- ``cycle_to_cycle[i] = period[i+1] - period[i]``; reported as ``n``, ``rms``,
  ``peak`` (largest absolute value).
- ``tie[i] = t[i] - (a + b*i)`` with ``(a, b)`` the least-squares line over
  every returned crossing; reported as ``n``, ``rms``, ``peak_to_peak`` and
  ``fit_period = k*b`` (the fitted full-cycle period).

A reduction without enough crossings is reported as ``{"omitted": "<reason>"}``,
never as zeros.
"""

from __future__ import annotations

import math
from typing import Any

from .sim_errors import SimError

#: The ``crossings.reduce`` names, in report order.
REDUCTIONS = ("period", "cycle_to_cycle", "tie")

#: The ``crossings.value_from`` selectors (issue #6913): which reduction the
#: entry's graded ``value`` reads instead of the crossing count. A closed
#: vocabulary -- an arbitrary dotted path into ``reductions`` is refused.
VALUE_FROM = ("period.mean", "frequency")

#: Reason token a ``value_from`` entry's ``measurement`` diagnostic leads
#: with when the selected reduction cannot be computed (too few edges in
#: the window), per the ``[<reason>]`` convention of ``sim_cycle_mean``.
INSUFFICIENT_EDGES_CODE = "crossings_insufficient_edges"


def validate_reduce(name: Any, reduce: Any) -> None:
    if reduce is None:
        return
    if not isinstance(reduce, list) or not all(isinstance(r, str) for r in reduce):
        raise SimError(
            f"measurement '{name}': crossings.reduce must be a list of "
            f"names drawn from {list(REDUCTIONS)} (got {reduce!r})"
        )
    unknown = sorted(set(reduce) - set(REDUCTIONS))
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown crossings.reduce name(s) "
            f"{unknown}; allowed: {list(REDUCTIONS)}"
        )


def validate_value_from(name: Any, value_from: Any) -> None:
    """Refuse, before any run, a ``crossings.value_from`` that is not one of
    :data:`VALUE_FROM` -- by name, so a typo cannot silently grade counts."""
    if value_from is None:
        return
    if not isinstance(value_from, str) or value_from not in VALUE_FROM:
        raise SimError(
            f"measurement '{name}': crossings.value_from must be one of "
            f"{list(VALUE_FROM)} (got {value_from!r}) -- it selects the "
            "reduction the entry's value grades instead of the crossing "
            "count (seconds for 'period.mean', Hz for 'frequency')"
        )


def graded_value_from(
    name: Any, reductions: dict[str, Any] | None, value_from: str
) -> tuple[float | None, str | None]:
    """The value a ``value_from`` selector grades: ``period.mean`` is the
    mean period in seconds, ``frequency`` is ``1 / period.mean`` in Hz.
    Returns ``(value, None)``, or ``(None, message)`` -- leading the
    ``[crossings_insufficient_edges]`` token -- when the ``period``
    reduction was omitted (too few edges in the window), so the entry is an
    error, never a small number."""
    period = (reductions or {}).get("period")
    if not isinstance(period, dict) or "omitted" in period:
        reason = period["omitted"] if isinstance(period, dict) else "was not computed"
        return None, (
            f"[{INSUFFICIENT_EDGES_CODE}] measurement '{name}': "
            f"crossings.period {reason} -- too few edges in the window to "
            f"grade value_from {value_from!r}; widen the crossings.from/to "
            "window or drop value_from to grade the edge count"
        )
    mean = float(period["mean"])
    return (mean if value_from == "period.mean" else 1.0 / mean), None


def _periods(times: list[float], step: int) -> list[float]:
    return [times[i + step] - times[i] for i in range(len(times) - step)]


def _period(times: list[float], step: int) -> dict[str, Any]:
    periods = _periods(times, step)
    if not periods:
        return _omitted(f"needs at least {step + 1} crossings (got {len(times)})")
    mean = math.fsum(periods) / len(periods)
    var = math.fsum((p - mean) ** 2 for p in periods) / len(periods)
    return {
        "n": len(periods),
        "mean": mean,
        "std": math.sqrt(var),
        "min": min(periods),
        "max": max(periods),
    }


def _cycle_to_cycle(times: list[float], step: int) -> dict[str, Any]:
    periods = _periods(times, step)
    if len(periods) < 2:
        return _omitted(f"needs at least {step + 2} crossings (got {len(times)})")
    diffs = [periods[i + 1] - periods[i] for i in range(len(periods) - 1)]
    return {
        "n": len(diffs),
        "rms": math.sqrt(math.fsum(d * d for d in diffs) / len(diffs)),
        "peak": max(abs(d) for d in diffs),
    }


def _tie(times: list[float], step: int) -> dict[str, Any]:
    n = len(times)
    if n < 3:
        return _omitted(f"needs at least 3 crossings (got {n})")
    mean_i = (n - 1) / 2
    mean_t = math.fsum(times) / n
    sxx = math.fsum((i - mean_i) ** 2 for i in range(n))
    slope = math.fsum((i - mean_i) * (times[i] - mean_t) for i in range(n)) / sxx
    intercept = mean_t - slope * mean_i
    resid = [times[i] - (intercept + slope * i) for i in range(n)]
    return {
        "n": n,
        "rms": math.sqrt(math.fsum(r * r for r in resid) / n),
        "peak_to_peak": max(resid) - min(resid),
        "fit_period": step * slope,
    }


def _omitted(reason: str) -> dict[str, Any]:
    return {"omitted": reason}


def reduce_crossings(
    times: list[float], edge: str, names: list[str]
) -> dict[str, dict[str, Any]]:
    """``{name: reduction}`` for each requested name over ``times``."""
    step = 2 if edge == "either" else 1
    funcs = {"period": _period, "cycle_to_cycle": _cycle_to_cycle, "tie": _tie}
    return {name: funcs[name](times, step) for name in REDUCTIONS if name in names}
