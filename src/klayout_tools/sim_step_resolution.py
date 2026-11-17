"""``measurements[].step_resolution`` for ``klt sim`` (issue #4773).

A switching-converter transient needs a small time step only near each
switching edge, but ``.tran`` takes one global ``tmax``. A request therefore
cannot *state or verify* the step the simulator actually took around an edge.
A ``measurements[]`` entry declared with ``step_resolution`` reports exactly
that -- the largest gap between consecutive time samples inside a window
around every crossing of a named signal::

    {"name": "edge_step",
     "step_resolution": {"signal": "v(sw)", "level": 2.5, "edge": "either",
                         "window": "10n"},
     "unit": "s",
     "limits": {"max": 1e-9}}

It is a **report only**: it reads the finished waveform and never changes the
solve (it cannot force a smaller step).

How it is computed
------------------
Like :mod:`klayout_tools.sim_crossings`, the reduction runs in Python inside
the corner run from a small ASCII rawfile the generated ``.control`` block
writes after the analysis::

    let __klt_stepres_<k> = <signal>
    write <corner_dir>/step_resolution_<k>.raw __klt_stepres_<k>

so ``local`` and ``remote``/``batch`` workers return the same numbers and only
the reduced values leave the corner.

Every (linearly interpolated) crossing of ``level`` is located with
:func:`klayout_tools.sim_crossings.find_crossings`. For each crossing at time
``t`` the **gap** is the largest ``time[i] - time[i-1]`` over the sample
intervals that overlap ``[t - window, t + window]`` -- an interval straddling
the window boundary counts, because it is a step the solver took across the
edge region.

Response shape (additive, no ``schema_version`` bump)
-----------------------------------------------------
``value`` is the **overall maximum gap in seconds** over all crossings kept,
so ``limits``/``plausible_range``/Monte Carlo statistics grade it like any
scalar. A sibling ``step_resolution`` object carries
``{max_gap, crossings: [{time, max_gap}, ...], count, truncated}`` (the list is
capped at ``max`` entries; ``max_gap`` covers all crossings). Every failure is
``status: "error"``, ``value: null`` with a ``code: "measurement"`` diagnostic
whose message starts with a ``[<reason>]`` token from :data:`ERROR_CODES`.
"""

from __future__ import annotations

import bisect
import math
import os
from dataclasses import dataclass, field
from typing import Any

from . import sim_crossings, sim_window
from .sim_errors import SimError
from .sim_forms import other_forms, validate_exclusive_form

#: Reason tokens a failed ``step_resolution`` entry's diagnostic leads with.
ERROR_CODES = (
    "step_resolution_vector_missing",
    "step_resolution_no_crossings",
)

_ALLOWED_KEYS = frozenset({"signal", "level", "edge", "window", "from", "to", "max"})

_VECTOR_PREFIX = "__klt_stepres_"


def is_step_resolution(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``step_resolution``."""
    return spec.get("step_resolution") is not None


def step_resolution_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``step_resolution`` entries, in declaration order -- also the order
    their deck lines and rawfiles are numbered in."""
    return [spec for spec in measurements_spec if is_step_resolution(spec)]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def _validate_exclusive_body(name: Any, spec: dict[str, Any]) -> None:
    if spec.get("optional") is True:
        raise SimError(
            f"measurement '{name}': 'optional' is not supported on "
            "'step_resolution' (a missing series is a read failure, not "
            "absent data)"
        )
    validate_step_resolution_spec(name, spec["step_resolution"])


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``step_resolution``: refuse any other form beside
    it and validate the body. Returns ``True`` for such an entry."""
    return validate_exclusive_form(
        spec,
        "step_resolution",
        other_forms("step_resolution"),
        _validate_exclusive_body,
    )


def _seconds(name: Any, body: dict[str, Any], key: str) -> float | None:
    return sim_window.window_bound(name, body, key, form="step_resolution")


def validate_step_resolution_spec(name: Any, body: Any) -> None:
    """Validate one ``measurements[].step_resolution`` object, raising
    :class:`~klayout_tools.sim.SimError` with a corrective message."""
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'step_resolution' must be an object, e.g. "
            '{"signal": "v(sw)", "level": 2.5, "window": "10n"}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown step_resolution key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    sim_crossings._validate_signal(name, body.get("signal"))
    sim_crossings._validate_level(name, body.get("level"))
    edge = body.get("edge", "either")
    if edge not in sim_crossings.CROSSING_EDGES:
        raise SimError(
            f"measurement '{name}': step_resolution.edge must be one of "
            f"{list(sim_crossings.CROSSING_EDGES)} (got {edge!r})"
        )
    window = _seconds(name, body, "window") if body.get("window") is not None else None
    if window is None or window <= 0:
        raise SimError(
            f"measurement '{name}': step_resolution.window is required and "
            "must be a positive half-width in seconds -- a number or a SPICE "
            f'time literal such as "10n" (got {body.get("window")!r})'
        )
    sim_crossings._validate_max(name, body.get("max"))
    start = _seconds(name, body, "from")
    stop = _seconds(name, body, "to")
    if start is not None and stop is not None and stop < start:
        raise SimError(
            f"measurement '{name}': step_resolution.to ({stop!r} s) is before "
            f"step_resolution.from ({start!r} s)"
        )


def validate_step_resolution_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A step gap is a transient-time quantity: refuse any other
    ``analysis.kind`` up front, by name."""
    declared = step_resolution_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "tran":
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'step_resolution', "
            "which reports transient time-step sizes and so requires "
            f'analysis.kind "tran" (got {analysis.get("kind")!r})'
        )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    """Where step_resolution measurement number ``index`` writes its rawfile."""
    return os.path.join(corner_dir, f"step_resolution_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """One ``let`` plus one ``write`` per entry, run after the analysis. Empty
    without a ``step_resolution`` entry (deck byte-identical to before)."""
    lines: list[str] = []
    for index, spec in enumerate(step_resolution_measurements(measurements_spec)):
        vector = f"{_VECTOR_PREFIX}{index}"
        lines.append(f"let {vector} = {spec['step_resolution']['signal']}")
        lines.append(f"write {rawfile_path(corner_dir, index)} {vector}")
    return lines


# --------------------------------------------------------------------------- #
# Reduction
# --------------------------------------------------------------------------- #


@dataclass
class StepResolutionOutcome:
    """One entry's result, or why there is none."""

    max_gap: float | None = None
    crossings: list[dict[str, float]] = field(default_factory=list)
    count: int = 0
    truncated: bool = False
    error: str | None = None
    code: str | None = None

    @property
    def value(self) -> float | None:
        return self.max_gap

    def report(self) -> dict[str, Any]:
        """The response's ``step_resolution`` object for this measurement."""
        return {
            "max_gap": self.max_gap,
            "crossings": [dict(c) for c in self.crossings],
            "count": self.count,
            "truncated": self.truncated,
        }


def window_max_gap(
    scale: list[float], center: float, half_width: float
) -> float | None:
    """Largest ``scale[i] - scale[i-1]`` over the sample intervals overlapping
    ``[center - half_width, center + half_width]``; ``None`` when none does.
    ``scale`` must be ascending."""
    lo, hi = center - half_width, center + half_width
    # First interval whose right end is past `lo`; stop at left end >= `hi`.
    first = max(bisect.bisect_right(scale, lo), 1)
    gap: float | None = None
    for i in range(first, len(scale)):
        if scale[i - 1] >= hi:
            break
        step = scale[i] - scale[i - 1]
        if gap is None or step > gap:
            gap = step
    return gap


def compute_step_resolution(
    scale: list[float],
    values: list[float],
    level: float,
    window: float,
    *,
    edge: str = "either",
    start: float | None = None,
    stop: float | None = None,
    max_count: int = sim_crossings.DEFAULT_MAX_CROSSINGS,
) -> StepResolutionOutcome:
    """The per-crossing and overall largest time-sample gap near the
    crossings of ``level`` by ``values`` over ``scale``."""
    found = sim_crossings.find_crossings(
        scale,
        values,
        level,
        edge=edge,
        start=start,
        stop=stop,
        max_count=len(scale),
    )
    if not found.times:
        return StepResolutionOutcome(
            code="step_resolution_no_crossings",
            error=(
                f"the signal never crosses {level!r} ({edge}) inside the "
                "window -- there is no edge to measure the step around"
            ),
        )
    outcome = StepResolutionOutcome(count=found.total)
    worst = 0.0
    for time in found.times:
        gap = window_max_gap(scale, time, window)
        if gap is None:
            continue
        worst = max(worst, gap)
        if len(outcome.crossings) < max_count:
            outcome.crossings.append({"time": time, "max_gap": gap})
    outcome.max_gap = worst
    outcome.truncated = outcome.count > len(outcome.crossings)
    return outcome


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_corner_step_resolutions(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, StepResolutionOutcome]:
    """``{position in measurements_spec: outcome}`` for every entry of one
    finished corner. Never raises: any rawfile problem becomes that
    measurement's own ``error``."""
    outcomes: dict[int, StepResolutionOutcome] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not is_step_resolution(spec):
            continue
        outcomes[position] = _extract_one(spec, rawfile_path(corner_dir, index))
        index += 1
    return outcomes


def _extract_one(spec: dict[str, Any], raw_path: str) -> StepResolutionOutcome:
    from .sim import parse_ascii_rawfile

    body = spec["step_resolution"]
    name = spec["name"]
    if not os.path.isfile(raw_path):
        return StepResolutionOutcome(
            code="step_resolution_vector_missing",
            error=(
                f"measurement '{name}': its signal ({body['signal']!r}) did "
                "not resolve against this transient's saved vectors (no "
                "rawfile was written) -- check the node/branch name"
            ),
        )
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError) as exc:
        return StepResolutionOutcome(
            code="step_resolution_vector_missing",
            error=f"measurement '{name}': could not read its rawfile: {exc}",
        )
    points = waveform["points"]
    if len(waveform["variables"]) < 2 or not points:
        return StepResolutionOutcome(
            code="step_resolution_vector_missing",
            error=f"measurement '{name}': its signal yielded no samples",
        )
    outcome = compute_step_resolution(
        [point[0] for point in points],
        [point[1] for point in points],
        float(body["level"]),
        float(_seconds(name, body, "window") or 0.0),
        edge=body.get("edge", "either"),
        start=_seconds(name, body, "from"),
        stop=_seconds(name, body, "to"),
        max_count=body.get("max") or sim_crossings.DEFAULT_MAX_CROSSINGS,
    )
    if outcome.error is not None:
        outcome.error = f"measurement '{name}': {outcome.error}"
    elif outcome.max_gap is None or not math.isfinite(outcome.max_gap):
        return StepResolutionOutcome(
            code="step_resolution_vector_missing",
            error=f"measurement '{name}': the step gap is not finite",
        )
    return outcome
