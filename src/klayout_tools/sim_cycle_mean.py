"""Cycle-aligned mean for ``klt sim`` (issue #2947).

A ``measurements[]`` entry declared with ``cycle_mean`` returns the mean of a
transient signal over a whole number of periods of a *reference* signal, the
window start found from the reference's own waveform::

    {"name": "vout_avg",
     "cycle_mean": {"signal": "v(out)", "align": "v(clk)", "level": 0.9,
                    "edge": "rise", "from": "10u", "cycles": 8}}

A fixed literal window cannot do this (it biases the mean by up to
ripple / cycles-in-window), and ngspice's ``.meas AVG ... TRIG/TARG`` and
``FROM=<earlier meas>`` are not available, so -- exactly like ``crossings``
(:mod:`klayout_tools.sim_crossings`, whose rawfile write/parse and
interpolated edge finder this reuses) -- the reduction runs in Python inside
the corner run, from a small per-measurement ASCII rawfile the generated
``.control`` block writes after the analysis::

    let __klt_cmean_<k>_sig = <signal>
    let __klt_cmean_<k>_ref = <align>
    write <corner_dir>/cycle_mean_<k>.raw __klt_cmean_<k>_sig __klt_cmean_<k>_ref

Only the reduced scalar leaves the corner, so ``remote``/``batch`` workers
work with ``options.keep_artifacts: false`` and no waveform artifact.

Averaging method
----------------
The sampled signal is a *non-uniform* timestep series, so a plain sample mean
is biased toward wherever the simulator took small steps. The mean is the
**time-weighted (trapezoidal) integral** of the piecewise-linear signal over
``[t_0, t_N]`` divided by the span ``t_N - t_0``, where ``t_0 .. t_N`` are the
first ``cycles + 1`` linearly-interpolated reference crossings at or after
``from``, and the signal is interpolated at those exact times.

Response shape (additive, no ``schema_version`` bump)
-----------------------------------------------------
``value`` is the mean (a number), so ``limits``/``plausible_range``/Monte
Carlo statistics/coverage grade it like any scalar. The window rides a
sibling ``cycle_mean`` object ``{t_start, t_end, cycles_used}`` on that
``corners[].measurements[]`` entry only, present only when a mean was
computed. Every failure is ``status: "error"``, ``value: null`` with a
``code: "measurement"`` diagnostic whose message starts with a
``[<reason>]`` token from :data:`ERROR_CODES`.
"""

from __future__ import annotations

import bisect
import math
import os
from dataclasses import dataclass
from typing import Any

from . import sim_crossings

#: Edge polarities that can align a cycle (``either`` would make "one cycle"
#: ambiguous, so it is refused).
CYCLE_EDGES = ("rise", "fall")

#: Largest ``cycles`` accepted -- bounds the window a request can ask for.
MAX_CYCLES = 1_000_000

#: Reason tokens a failed ``cycle_mean`` entry's diagnostic message leads with.
ERROR_CODES = (
    "cycle_mean_vector_missing",
    "cycle_mean_no_reference_crossing",
    "cycle_mean_insufficient_edges",
)

_ALLOWED_KEYS = frozenset({"signal", "align", "level", "edge", "from", "cycles"})

_VECTOR_PREFIX = "__klt_cmean_"


def _error(message: str) -> Exception:
    from .sim import SimError

    return SimError(message)


def is_cycle_mean(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``cycle_mean``."""
    return spec.get("cycle_mean") is not None


def cycle_mean_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``cycle_mean`` entries, in declaration order -- also the order
    their deck lines and rawfiles are numbered in."""
    return [spec for spec in measurements_spec if is_cycle_mean(spec)]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_cycle_mean_spec(name: Any, body: Any) -> None:
    """Validate one ``measurements[].cycle_mean`` object, raising
    :class:`~klayout_tools.sim.SimError` with a corrective message."""
    if not isinstance(body, dict):
        raise _error(
            f"measurement '{name}': 'cycle_mean' must be an object, e.g. "
            '{"signal": "v(out)", "align": "v(clk)", "level": 0.9, '
            '"cycles": 8}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise _error(
            f"measurement '{name}': unknown cycle_mean key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    for key in ("signal", "align"):
        sim_crossings._validate_signal(name, body.get(key))
    sim_crossings._validate_level(name, body.get("level"))
    edge = body.get("edge", "rise")
    if edge not in CYCLE_EDGES:
        raise _error(
            f"measurement '{name}': cycle_mean.edge must be one of "
            f"{list(CYCLE_EDGES)} (got {edge!r})"
        )
    cycles = body.get("cycles")
    if (
        not isinstance(cycles, int)
        or isinstance(cycles, bool)
        or not 1 <= cycles <= MAX_CYCLES
    ):
        raise _error(
            f"measurement '{name}': cycle_mean.cycles is required and must be "
            f"an integer in [1, {MAX_CYCLES}] (got {cycles!r})"
        )
    sim_crossings._window_bound(name, body, "from")


def validate_cycle_mean_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A cycle-aligned mean is a transient-time quantity: refuse it for any
    other ``analysis.kind`` up front, by name."""
    declared = cycle_mean_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "tran":
        raise _error(
            f"measurement '{declared[0]['name']}' declares 'cycle_mean', "
            "which averages a transient signal over reference cycles and so "
            f'requires analysis.kind "tran" (got {analysis.get("kind")!r})'
        )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    """Where cycle_mean measurement number ``index`` writes its rawfile."""
    return os.path.join(corner_dir, f"cycle_mean_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """The ``.control`` lines that dump each cycle_mean signal pair to its own
    ASCII rawfile, run after the analysis. One file per measurement, so a
    vector that does not resolve costs only its own measurement. Empty when
    no cycle_mean entry is declared (deck byte-identical to before)."""
    lines: list[str] = []
    for index, spec in enumerate(cycle_mean_measurements(measurements_spec)):
        body = spec["cycle_mean"]
        sig = f"{_VECTOR_PREFIX}{index}_sig"
        ref = f"{_VECTOR_PREFIX}{index}_ref"
        lines.append(f"let {sig} = {body['signal']}")
        lines.append(f"let {ref} = {body['align']}")
        lines.append(f"write {rawfile_path(corner_dir, index)} {sig} {ref}")
    return lines


# --------------------------------------------------------------------------- #
# Reduction
# --------------------------------------------------------------------------- #


@dataclass
class CycleMeanOutcome:
    """One cycle_mean measurement's result, or why there is none."""

    mean: float | None = None
    t_start: float | None = None
    t_end: float | None = None
    cycles_used: int = 0
    error: str | None = None
    code: str | None = None

    def report(self) -> dict[str, Any]:
        """The response's ``cycle_mean`` object for this measurement."""
        return {
            "t_start": self.t_start,
            "t_end": self.t_end,
            "cycles_used": self.cycles_used,
        }


def _value_at(scale: list[float], values: list[float], t: float) -> float:
    """Linear interpolation of the sampled signal at ``t`` (inside the scale)."""
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


def time_weighted_mean(
    scale: list[float], values: list[float], t_start: float, t_end: float
) -> float:
    """Mean of the piecewise-linear signal over ``[t_start, t_end]``:
    trapezoidal integral with both endpoints interpolated at their exact
    times, divided by the span. ``t_end`` must exceed ``t_start``."""
    knots = [t_start]
    knots.extend(t for t in scale if t_start < t < t_end)
    knots.append(t_end)
    area = 0.0
    previous = _value_at(scale, values, knots[0])
    for left, right in zip(knots, knots[1:], strict=False):
        current = _value_at(scale, values, right)
        area += 0.5 * (previous + current) * (right - left)
        previous = current
    return area / (t_end - t_start)


def compute_cycle_mean(
    scale: list[float],
    signal: list[float],
    reference: list[float],
    *,
    level: float,
    edge: str = "rise",
    start: float | None = None,
    cycles: int,
) -> CycleMeanOutcome:
    """The cycle-aligned mean, or an outcome carrying ``code``/``error``."""
    edges = sim_crossings.find_crossings(
        scale, reference, level, edge=edge, start=start, max_count=cycles + 1
    )
    if edges.total == 0:
        return CycleMeanOutcome(
            code="cycle_mean_no_reference_crossing",
            error=(
                f"the reference never crosses {level!r} ({edge}) at or after "
                f"{start if start is not None else 0.0!r} s -- no cycle to "
                "align to"
            ),
        )
    if len(edges.times) < cycles + 1:
        return CycleMeanOutcome(
            code="cycle_mean_insufficient_edges",
            error=(
                f"only {edges.total} {edge} reference edge(s) found at or "
                f"after {start if start is not None else 0.0!r} s; "
                f"{cycles} cycle(s) need {cycles + 1}"
            ),
        )
    t_start, t_end = edges.times[0], edges.times[cycles]
    return CycleMeanOutcome(
        mean=time_weighted_mean(scale, signal, t_start, t_end),
        t_start=t_start,
        t_end=t_end,
        cycles_used=cycles,
    )


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_corner_cycle_means(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, CycleMeanOutcome]:
    """``{position in measurements_spec: outcome}`` for every cycle_mean entry
    of one finished corner, read from the rawfiles :func:`control_lines` asked
    ngspice to write. Never raises: an unreadable or missing rawfile becomes
    that measurement's own ``error``."""
    outcomes: dict[int, CycleMeanOutcome] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not is_cycle_mean(spec):
            continue
        outcomes[position] = _extract_one(spec, rawfile_path(corner_dir, index))
        index += 1
    return outcomes


def _extract_one(spec: dict[str, Any], raw_path: str) -> CycleMeanOutcome:
    from .sim import SimError, parse_ascii_rawfile

    body = spec["cycle_mean"]
    name = spec["name"]
    if not os.path.isfile(raw_path):
        return CycleMeanOutcome(
            code="cycle_mean_vector_missing",
            error=(
                f"measurement '{name}': its signal ({body['signal']!r}) or "
                f"align ({body['align']!r}) did not resolve against this "
                "transient's saved vectors (no rawfile was written) -- check "
                "the node/branch names"
            ),
        )
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError) as exc:
        return CycleMeanOutcome(
            code="cycle_mean_vector_missing",
            error=f"measurement '{name}': could not read its cycle_mean rawfile: {exc}",
        )
    points = waveform["points"]
    if len(waveform["variables"]) < 3 or not points:
        return CycleMeanOutcome(
            code="cycle_mean_vector_missing",
            error=f"measurement '{name}': its signal/align vectors yielded no samples",
        )
    outcome = compute_cycle_mean(
        [point[0] for point in points],
        [point[1] for point in points],
        [point[2] for point in points],
        level=float(body["level"]),
        edge=body.get("edge", "rise"),
        start=sim_crossings._window_bound(name, body, "from"),
        cycles=body["cycles"],
    )
    if outcome.error is not None:
        outcome.error = f"measurement '{name}': {outcome.error}"
    elif outcome.mean is not None and not math.isfinite(outcome.mean):
        return CycleMeanOutcome(
            code="cycle_mean_vector_missing",
            error=f"measurement '{name}': the averaged signal is not finite",
        )
    return outcome
