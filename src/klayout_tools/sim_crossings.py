"""Threshold-crossing time series for ``klt sim`` (issue #2868).

A ``measurements[]`` entry declared with ``crossings`` returns the **whole
ordered list** of times a transient signal crosses a level -- the edge list a
cycle-by-cycle period, jitter or lock/slip reduction consumes -- from one
declaration, instead of one ``.meas ... when v(x)=<thr> rise=<k>`` card per
edge::

    {"name": "t_ro1",
     "crossings": {"signal": "v(ro1)", "level": 0.9, "edge": "rise",
                   "from": "10n", "max": 200}}

How it is computed, and why there
---------------------------------
The series is extracted **inside the corner run**, in Python, from a small
per-measurement ASCII rawfile the generated ``.control`` block writes right
after the analysis (``let __klt_xing_<k> = <signal>`` /
``write <corner_dir>/crossings_<k>.raw __klt_xing_<k>`` -- the plot's scale
vector, ``time``, comes along automatically). So:

- only the reduced array crosses an off-host boundary: a ``remote``/``batch``
  worker runs the same corner code, and its report carries the series inline,
  with ``options.keep_artifacts: false`` and no waveform artifact at all;
- the rawfile lives in the corner's own working directory and is removed with
  it when artifacts are not kept.

Crossing times are **linearly interpolated** between the two samples that
bracket the level -- nearest-sample extraction would report the simulator's
timestep as jitter (issue #56's argument, absorbed here). Optional reductions
over the series (``crossings.reduce``: period, cycle-to-cycle, TIE) live in
:mod:`klayout_tools.sim_crossings_reduce` (issue #56).

Response shape (additive, no ``schema_version`` bump)
-----------------------------------------------------
The measurement's ``value`` stays a **number**: the count of crossings found
inside the window (before the ``max`` cap), so ``limits``/``plausible_range``
grade it like any scalar (e.g. ``limits.min: 40`` = "at least 40 edges"), the
Monte Carlo statistics and coverage layers need no change, and an unaware
consumer of ``value`` keeps reading a number. The series rides a new sibling
``crossings`` object on that ``corners[].measurements[]`` entry only, present
only when the series was extracted -- the same "new optional member, absent
when not applicable" rule ``docs/json-contract.md`` records for additive
fields.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any

from . import sim_supply_relative, sim_window
from ._jsonnum import is_number
from .sim_crossings_reduce import reduce_crossings, validate_reduce
from .sim_errors import SimError

#: The ``crossings.edge`` values: a rising crossing, a falling one, or both.
CROSSING_EDGES = ("rise", "fall", "either")

#: ``crossings.max`` when the request omits it, and the largest value it may
#: take -- both bound the response size per measurement per corner.
DEFAULT_MAX_CROSSINGS = 1000
MAX_CROSSINGS_CAP = 100_000

#: Keys a ``crossings`` object may carry. Anything else is refused by name
#: (a typo'd ``form``/``tmax`` must not silently widen the window).
_ALLOWED_KEYS = frozenset({"signal", "level", "edge", "from", "to", "max", "reduce"})

#: Prefix of the ngspice vector each crossings measurement is bound to in the
#: generated deck -- never a caller-visible name.
_VECTOR_PREFIX = "__klt_xing_"


def is_crossings(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``crossings``."""
    return spec.get("crossings") is not None


def crossings_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``crossings`` entries, in declaration order -- which is also the
    order their deck lines and rawfiles are numbered in."""
    return [spec for spec in measurements_spec if is_crossings(spec)]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_crossings_spec(name: Any, body: Any) -> None:
    """Validate one ``measurements[].crossings`` object, raising
    :class:`~klayout_tools.sim.SimError` with a corrective message."""
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'crossings' must be an object, e.g. "
            '{"signal": "v(out)", "level": 0.9, "edge": "rise"}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown crossings key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    _validate_signal(name, body.get("signal"))
    _validate_crossings_level(name, body.get("level"))
    edge = body.get("edge", "rise")
    if edge not in CROSSING_EDGES:
        raise SimError(
            f"measurement '{name}': crossings.edge must be one of "
            f"{list(CROSSING_EDGES)} (got {edge!r})"
        )
    _validate_max(name, body.get("max"))
    validate_reduce(name, body.get("reduce"))
    start = sim_window.window_bound(name, body, "from", form="crossings")
    stop = sim_window.window_bound(name, body, "to", form="crossings")
    if start is not None and stop is not None and stop < start:
        raise SimError(
            f"measurement '{name}': crossings.to ({stop!r} s) is before "
            f"crossings.from ({start!r} s)"
        )


def _validate_signal(name: Any, signal: Any) -> None:
    if not isinstance(signal, str) or not signal.strip():
        raise SimError(
            f"measurement '{name}': crossings.signal is required -- a "
            'single-line ngspice vector expression such as "v(out)" or '
            '"v(outp) - v(outn)"'
        )
    if "\n" in signal or "\r" in signal:
        raise SimError(
            f"measurement '{name}': crossings.signal must be a single line "
            "(it is spliced into one `let` command of the generated "
            ".control block)"
        )


def _validate_level(name: Any, level: Any) -> None:
    if (
        not isinstance(level, (int, float))
        or isinstance(level, bool)
        or not math.isfinite(level)
    ):
        raise SimError(
            f"measurement '{name}': crossings.level is required and must be "
            f"a finite number in the signal's own unit (got {level!r})"
        )


def is_sampled_offset(level: Any) -> bool:
    """Whether ``level`` is the sampled-offset form ``{sample_at, offset}``
    (issue #4813): the signal's own value at a time, plus an offset."""
    return isinstance(level, dict) and ("sample_at" in level or "offset" in level)


def _validate_sampled_offset(name: Any, level: dict[str, Any]) -> None:
    where = f"measurement '{name}': crossings.level"
    if set(level) != {"sample_at", "offset"}:
        raise SimError(
            f"{where} sampled-offset form takes exactly the keys "
            '"sample_at" and "offset", e.g. {"sample_at": "50n", '
            f'"offset": -0.9}} (got keys {sorted(level)})'
        )
    sim_window.window_bound(
        name, level, "sample_at", form="crossings", field="level.sample_at"
    )
    offset = level["offset"]
    if not is_number(offset) or not math.isfinite(offset):
        raise SimError(
            f"{where}.offset must be a finite number in the signal's own "
            f"unit (got {offset!r})"
        )


def _validate_crossings_level(name: Any, level: Any) -> None:
    """``crossings.level``: a finite number, a ``{sample_at, offset}``
    sampled-offset level (issue #4813), or (issue #3918) a
    ``{frac_of, fraction}`` fraction of a ``corners.supply_v`` key resolved per
    corner. Whether the key exists is checked once the corners are known."""
    if is_sampled_offset(level):
        _validate_sampled_offset(name, level)
        return
    if not sim_supply_relative.is_supply_relative(level):
        _validate_level(name, level)
        return
    try:
        sim_supply_relative.validate(level, f"measurement '{name}': crossings.level")
    except sim_supply_relative.SupplyRelativeError as exc:
        raise SimError(str(exc)) from exc


def validate_supply_relative_levels(
    measurements_spec: list[dict[str, Any]], supply_maps: list[dict[str, Any]]
) -> None:
    """Refuse, before any run, a supply-relative ``crossings.level`` whose
    ``frac_of`` key one of the corners' ``supply_v`` maps does not carry."""
    for spec in crossings_measurements(measurements_spec):
        level = spec["crossings"].get("level")
        if not sim_supply_relative.is_supply_relative(level):
            continue
        for supply_v in supply_maps:
            try:
                sim_supply_relative.resolve(
                    level, f"measurement '{spec['name']}': crossings.level", supply_v
                )
            except sim_supply_relative.SupplyRelativeError as exc:
                raise SimError(str(exc)) from exc


def _validate_max(name: Any, value: Any) -> None:
    if value is None:
        return
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_CROSSINGS_CAP
    ):
        raise SimError(
            f"measurement '{name}': crossings.max must be an integer in "
            f"[1, {MAX_CROSSINGS_CAP}] (got {value!r}); omit it for the "
            f"default of {DEFAULT_MAX_CROSSINGS}"
        )


def validate_crossings_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A crossings series is a transient-time quantity: refuse it for any
    other ``analysis.kind`` up front, by name."""
    declared = crossings_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "tran":
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'crossings', which "
            "returns threshold-crossing *times* and so requires "
            f'analysis.kind "tran" (got {analysis.get("kind")!r})'
        )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    """Where crossings measurement number ``index`` writes its rawfile."""
    return os.path.join(corner_dir, f"crossings_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """The ``.control`` lines that dump each crossings signal to its own
    ASCII rawfile, run after the analysis. One file per measurement, so a
    signal that does not resolve costs only its own measurement. Empty when
    no crossings entry is declared (deck byte-identical to before)."""
    lines: list[str] = []
    for index, spec in enumerate(crossings_measurements(measurements_spec)):
        vector = f"{_VECTOR_PREFIX}{index}"
        lines.append(f"let {vector} = {spec['crossings']['signal']}")
        lines.append(f"write {rawfile_path(corner_dir, index)} {vector}")
    return lines


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@dataclass
class CrossingsOutcome:
    """One crossings measurement's extracted series, or why there is none."""

    times: list[float] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    error: str | None = None
    #: The threshold in volts, set only when ``level`` was supply-relative.
    resolved_level: float | None = None
    #: ``crossings.reduce`` results, set only when requested.
    reductions: dict[str, Any] | None = None

    def report(self) -> dict[str, Any]:
        """The response's ``crossings`` object for this measurement."""
        out: dict[str, Any] = {
            "times": list(self.times),
            "edges": list(self.edges),
            "truncated": self.truncated,
        }
        if self.resolved_level is not None:
            out["level"] = self.resolved_level
        if self.reductions is not None:
            out["reductions"] = self.reductions
        return out


def find_crossings(
    scale: list[float],
    values: list[float],
    level: float,
    *,
    edge: str = "rise",
    start: float | None = None,
    stop: float | None = None,
    max_count: int = DEFAULT_MAX_CROSSINGS,
) -> CrossingsOutcome:
    """Every crossing of ``level`` by ``values`` over ``scale``, linearly
    interpolated, in scale order.

    A rising crossing is a segment with ``y0 < level <= y1`` and a falling
    one ``y0 > level >= y1``: a sample landing exactly on the level is
    counted once (on the segment that arrives at it), and a signal that
    merely starts above (or below) the level contributes no crossing at the
    first sample. A crossing is kept when ``start <= t <= stop`` (either
    bound optional). ``total`` counts every kept crossing; only the first
    ``max_count`` are returned, with ``truncated`` set when that dropped
    some."""
    outcome = CrossingsOutcome()
    for index in range(1, min(len(scale), len(values))):
        direction = _segment_direction(values[index - 1], values[index], level)
        if direction is None or (edge != "either" and direction != edge):
            continue
        time = _interpolate(scale, values, index, level)
        if (start is not None and time < start) or (stop is not None and time > stop):
            continue
        outcome.total += 1
        if len(outcome.times) < max_count:
            outcome.times.append(time)
            outcome.edges.append(direction)
    outcome.truncated = outcome.total > len(outcome.times)
    return outcome


def _segment_direction(y0: float, y1: float, level: float) -> str | None:
    if y0 < level <= y1:
        return "rise"
    if y0 > level >= y1:
        return "fall"
    return None


def _interpolate(
    scale: list[float], values: list[float], index: int, level: float
) -> float:
    t0, t1 = scale[index - 1], scale[index]
    y0, y1 = values[index - 1], values[index]
    # y1 != y0 is guaranteed by the strict inequality in `_segment_direction`.
    return t0 + (level - y0) * (t1 - t0) / (y1 - y0)


def _resolve_sampled_offset(
    name: Any, level: dict[str, Any], scale: list[float], values: list[float]
) -> float:
    """``signal(sample_at) + offset``, the sample linearly interpolated between
    its two bracketing points. Raises ``ValueError`` (message = the
    measurement diagnostic) when ``sample_at`` is outside the simulated range."""
    at = sim_window.window_bound(
        name, level, "sample_at", form="crossings", field="level.sample_at"
    )
    assert at is not None  # validated as required before any run
    count = min(len(scale), len(values))
    if count == 0 or not scale[0] <= at <= scale[count - 1]:
        span = f"[{scale[0]!r}, {scale[count - 1]!r}] s" if count else "empty"
        raise ValueError(
            f"measurement '{name}': crossings.level.sample_at ({at!r} s) is "
            f"outside the simulated time range {span}"
        )
    sampled = values[count - 1]
    for index in range(1, count):
        if at <= scale[index]:
            t0, t1 = scale[index - 1], scale[index]
            y0, y1 = values[index - 1], values[index]
            sampled = y1 if t1 == t0 else y0 + (y1 - y0) * (at - t0) / (t1 - t0)
            break
    return sampled + float(level["offset"])


def extract_corner_crossings(
    measurements_spec: list[dict[str, Any]],
    corner_dir: str,
    supply_v: dict[str, Any] | None = None,
) -> dict[int, CrossingsOutcome]:
    """``{position in measurements_spec: outcome}`` for every crossings entry
    of one finished corner (``supply_v``: its supply map, which a
    supply-relative ``level`` is resolved against), read from the rawfiles
    :func:`control_lines` asked ngspice to write. Never raises: an unreadable
    or missing rawfile becomes that measurement's own ``error``."""
    outcomes: dict[int, CrossingsOutcome] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not is_crossings(spec):
            continue
        outcomes[position] = _extract_one(
            spec, rawfile_path(corner_dir, index), supply_v
        )
        index += 1
    return outcomes


def _extract_one(
    spec: dict[str, Any], raw_path: str, supply_v: dict[str, Any] | None = None
) -> CrossingsOutcome:
    from .sim import SimError, parse_ascii_rawfile

    body = spec["crossings"]
    signal = body["signal"]
    if not os.path.isfile(raw_path):
        return CrossingsOutcome(
            error=(
                f"measurement '{spec['name']}' produced no crossings series: "
                f"its signal ({signal!r}) did not resolve against this "
                "transient's saved vectors (no rawfile was written) -- check "
                "the node/branch name"
            )
        )
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError) as exc:
        return CrossingsOutcome(
            error=f"measurement '{spec['name']}': could not read its crossings "
            f"rawfile: {exc}"
        )
    points = waveform["points"]
    if len(waveform["variables"]) < 2 or not points:
        return CrossingsOutcome(
            error=(
                f"measurement '{spec['name']}' produced no crossings series: "
                f"its signal ({signal!r}) yielded no samples"
            )
        )
    name = spec["name"]
    raw_level = body["level"]
    resolved_level: float | None = None
    if is_sampled_offset(raw_level):
        try:
            resolved_level = _resolve_sampled_offset(
                name,
                raw_level,
                [point[0] for point in points],
                [point[1] for point in points],
            )
        except ValueError as exc:
            return CrossingsOutcome(error=str(exc))
    elif sim_supply_relative.is_supply_relative(raw_level):
        try:
            resolved_level = sim_supply_relative.resolve(
                raw_level, f"measurement '{name}': crossings.level", supply_v
            )
        except sim_supply_relative.SupplyRelativeError as exc:
            return CrossingsOutcome(error=str(exc))
    outcome = find_crossings(
        [point[0] for point in points],
        [point[1] for point in points],
        resolved_level if resolved_level is not None else float(raw_level),
        edge=body.get("edge", "rise"),
        start=sim_window.window_bound(name, body, "from", form="crossings"),
        stop=sim_window.window_bound(name, body, "to", form="crossings"),
        max_count=body.get("max") or DEFAULT_MAX_CROSSINGS,
    )
    outcome.resolved_level = resolved_level
    if body.get("reduce"):
        outcome.reductions = reduce_crossings(
            outcome.times, body.get("edge", "rise"), body["reduce"]
        )
    return outcome
