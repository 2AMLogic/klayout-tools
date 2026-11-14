"""``measurements[].settled`` for ``klt sim`` (issue #4780).

A switching-converter bench needs a "settled" verdict from a long ``.tran``:
the last two windows of N clock cycles must agree on the mean of one node and
the peak of another within given percentages. A ``measurements[]`` entry
declared with ``settled`` grades exactly that::

    {"name": "settled",
     "settled": {"clock": "v(clk)", "level": 0.9, "edge": "rise", "cycles": 8,
                 "mean": {"signal": "v(vout)", "tol_pct": 1.0},
                 "peak": {"signal": "i(l1)",  "tol_pct": 5.0},
                 "from": 1e-4}}

Windows
-------
A window is ``cycles`` consecutive clock cycles aligned on the ``clock``
edges at ``level``. Windows run back to back from ``from`` (default: the first
edge): window ``k`` spans edges ``k*cycles .. (k+1)*cycles``. Only complete
windows count. Each reports the time-weighted (trapezoidal) **mean** of
``mean.signal`` (same method as :mod:`klayout_tools.sim_cycle_mean`) and the
**peak** (maximum absolute value) of ``peak.signal`` over its span.

Verdict
-------
Only the last two windows are compared::

    |mean_n - mean_(n-1)| / |mean_n| * 100 <= mean.tol_pct
    |peak_n - peak_(n-1)| / |peak_n| * 100 <= peak.tol_pct

The denominator is the *last* window's value. When that value is exactly 0 the
relative delta is 0 if the two windows are identical and infinite otherwise
(there is no absolute fallback: a signal whose mean is ~0 only settles when
it is exactly flat; compare it through its peak instead). An infinite delta
is reported as ``null`` in the response (JSON has no infinity).

How it is computed
------------------
Like ``cycle_mean`` the reduction runs in Python inside the corner run from a
small ASCII rawfile the ``.control`` block writes after the analysis::

    let __klt_settled_<k>_clk = <clock>
    let __klt_settled_<k>_mean = <mean.signal>
    let __klt_settled_<k>_peak = <peak.signal>
    write <corner_dir>/settled_<k>.raw <the three vectors>

so ``local`` and ``remote``/``batch`` workers return the same numbers.

Response shape (additive, no ``schema_version`` bump)
-----------------------------------------------------
``value`` is ``1.0`` when settled and ``0.0`` when not, so "not settled" is a
graded result (``limits: {"min": 1}``) rather than an error. A sibling
``settled`` object on that ``corners[].measurements[]`` entry carries
``{settled, worst_delta_pct, mean_delta_pct, peak_delta_pct, windows_total,
windows: [{t_start, t_end, mean, peak}, ...]}`` (``windows`` keeps the last
:data:`MAX_REPORTED_WINDOWS`). Every failure is ``status: "error"``,
``value: null`` with a ``code: "measurement"`` diagnostic whose message starts
with a ``[<reason>]`` token from :data:`ERROR_CODES`.
"""

from __future__ import annotations

import bisect
import math
import os
from dataclasses import dataclass, field
from typing import Any

from . import sim_crossings, sim_cycle_mean, sim_measurement_kind, sim_window
from .sim_errors import SimError

#: Reason tokens a failed ``settled`` entry's diagnostic message leads with.
ERROR_CODES = (
    "settled_vector_missing",
    "settled_no_clock_crossing",
    "settled_insufficient_windows",
)

#: Fewest complete windows a verdict needs (the last two are compared, and a
#: third guarantees the run was not judged on its start-up transient alone).
MIN_WINDOWS = 3

#: How many trailing windows the response lists.
MAX_REPORTED_WINDOWS = 100

_ALLOWED_KEYS = frozenset({"clock", "level", "edge", "cycles", "mean", "peak", "from"})
_SIGNAL_KEYS = frozenset({"signal", "tol_pct"})

_VECTOR_PREFIX = "__klt_settled_"

#: Every other measurement form an entry must not also declare.
_FORMS = (
    "spice",
    "expr",
    "crossings",
    "cycle_mean",
    "energy",
    "stress",
    "noise",
    "transfer",
    "tian",
    "snm",
    "search",
    "eye",
    "step_resolution",
)


def is_settled(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``settled``."""
    return sim_measurement_kind.declares(spec, "settled")


def settled_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``settled`` entries, in declaration order -- also the order their
    deck lines and rawfiles are numbered in."""
    return [spec for spec in measurements_spec if is_settled(spec)]


def signal_texts(spec: dict[str, Any]) -> list[Any]:
    """The signal expressions a ``settled`` entry references (clock plus the
    mean/peak signals), for ``options.save_vectors`` auto-detection."""
    settled = spec.get("settled")
    if not isinstance(settled, dict):
        return []
    texts: list[Any] = [settled.get("clock")]
    for key in ("mean", "peak"):
        tracked = settled.get(key)
        if isinstance(tracked, dict):
            texts.append(tracked.get("signal"))
    return texts


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def _validate_tracked(name: Any, key: str, body: Any) -> None:
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': settled.{key} is required and must be an "
            'object, e.g. {"signal": "v(vout)", "tol_pct": 1.0}'
        )
    unknown = sorted(set(body) - _SIGNAL_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown settled.{key} key(s) {unknown}; "
            f"allowed: {sorted(_SIGNAL_KEYS)}"
        )
    sim_crossings._validate_signal(name, body.get("signal"))
    tol = body.get("tol_pct")
    if (
        isinstance(tol, bool)
        or not isinstance(tol, (int, float))
        or not math.isfinite(tol)
        or tol < 0
    ):
        raise SimError(
            f"measurement '{name}': settled.{key}.tol_pct is required and "
            f"must be a finite number >= 0, in percent (got {tol!r})"
        )


def validate_settled_spec(name: Any, body: Any) -> None:
    """Validate one ``measurements[].settled`` object, raising
    :class:`~klayout_tools.sim.SimError` with a corrective message."""
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'settled' must be an object, e.g. "
            '{"clock": "v(clk)", "level": 0.9, "cycles": 8, '
            '"mean": {"signal": "v(vout)", "tol_pct": 1.0}, '
            '"peak": {"signal": "i(l1)", "tol_pct": 5.0}}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown settled key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    sim_crossings._validate_signal(name, body.get("clock"))
    sim_crossings._validate_level(name, body.get("level"))
    edge = body.get("edge", "rise")
    if edge not in sim_cycle_mean.CYCLE_EDGES:
        raise SimError(
            f"measurement '{name}': settled.edge must be one of "
            f"{list(sim_cycle_mean.CYCLE_EDGES)} (got {edge!r})"
        )
    cycles = body.get("cycles")
    if (
        not isinstance(cycles, int)
        or isinstance(cycles, bool)
        or not 1 <= cycles <= sim_cycle_mean.MAX_CYCLES
    ):
        raise SimError(
            f"measurement '{name}': settled.cycles is required and must be "
            f"an integer in [1, {sim_cycle_mean.MAX_CYCLES}] (got {cycles!r})"
        )
    _validate_tracked(name, "mean", body.get("mean"))
    _validate_tracked(name, "peak", body.get("peak"))
    sim_window.window_bound(name, body, "from", form="settled")


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``settled``: refuse any other form beside it and
    validate the body. Returns ``True`` for such an entry."""
    if not is_settled(spec):
        return False
    name = spec.get("name")
    if name is None:
        raise SimError("each request.measurements[] entry requires 'name'")
    others = [form for form in _FORMS if spec.get(form) is not None]
    if others:
        raise SimError(
            f"measurement '{name}' declares 'settled' together with "
            f"'{others[0]}': they are alternative forms of a measurement -- "
            "declare exactly one (use a separate measurements[] entry for "
            "the other)"
        )
    if spec.get("optional") is True:
        raise SimError(
            f"measurement '{name}': 'optional' is not supported on "
            "'settled' (a missing series is a read failure, not absent data)"
        )
    validate_settled_spec(name, spec["settled"])
    return True


def validate_settled_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A settled verdict is a transient-time quantity: refuse any other
    ``analysis.kind`` up front, by name."""
    declared = settled_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "tran":
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'settled', which "
            "compares transient windows and so requires "
            f'analysis.kind "tran" (got {analysis.get("kind")!r})'
        )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    """Where settled measurement number ``index`` writes its rawfile."""
    return os.path.join(corner_dir, f"settled_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """The ``.control`` lines that dump each settled entry's clock, mean
    signal and peak signal to its own ASCII rawfile, run after the analysis.
    Empty without a ``settled`` entry (deck byte-identical to before)."""
    lines: list[str] = []
    for index, spec in enumerate(settled_measurements(measurements_spec)):
        body = spec["settled"]
        names = [f"{_VECTOR_PREFIX}{index}_{part}" for part in ("clk", "mean", "peak")]
        lines.append(f"let {names[0]} = {body['clock']}")
        lines.append(f"let {names[1]} = {body['mean']['signal']}")
        lines.append(f"let {names[2]} = {body['peak']['signal']}")
        lines.append(f"write {rawfile_path(corner_dir, index)} {' '.join(names)}")
    return lines


# --------------------------------------------------------------------------- #
# Reduction
# --------------------------------------------------------------------------- #


@dataclass
class SettledOutcome:
    """One settled measurement's result, or why there is none."""

    settled: bool | None = None
    mean_delta_pct: float | None = None
    peak_delta_pct: float | None = None
    windows_total: int = 0
    windows: list[dict[str, float]] = field(default_factory=list)
    error: str | None = None
    code: str | None = None

    @property
    def value(self) -> float | None:
        if self.settled is None:
            return None
        return 1.0 if self.settled else 0.0

    @property
    def worst_delta_pct(self) -> float | None:
        if self.mean_delta_pct is None or self.peak_delta_pct is None:
            return None
        return max(self.mean_delta_pct, self.peak_delta_pct)

    def report(self) -> dict[str, Any]:
        """The response's ``settled`` object for this measurement."""

        def finite(delta: float | None) -> float | None:
            # An infinite delta (zero last-window value) is not valid JSON.
            return delta if delta is not None and math.isfinite(delta) else None

        return {
            "settled": self.settled,
            "worst_delta_pct": finite(self.worst_delta_pct),
            "mean_delta_pct": finite(self.mean_delta_pct),
            "peak_delta_pct": finite(self.peak_delta_pct),
            "windows_total": self.windows_total,
            "windows": [dict(window) for window in self.windows],
        }


def window_peak(
    scale: list[float], values: list[float], t_start: float, t_end: float
) -> float:
    """Maximum ``|value|`` of the piecewise-linear signal over
    ``[t_start, t_end]``, endpoints interpolated at their exact times."""
    peak = max(
        abs(sim_window.value_at(scale, values, t_start)),
        abs(sim_window.value_at(scale, values, t_end)),
    )
    lo = bisect.bisect_right(scale, t_start)
    hi = bisect.bisect_left(scale, t_end)
    for index in range(lo, hi):
        peak = max(peak, abs(values[index]))
    return peak


def relative_delta_pct(current: float, previous: float) -> float:
    """``|current - previous| / |current|`` in percent; a zero ``current``
    gives 0 for an identical ``previous`` and infinity otherwise."""
    delta = abs(current - previous)
    if current == 0.0:
        return 0.0 if delta == 0.0 else math.inf
    return delta / abs(current) * 100.0


def compute_settled(
    scale: list[float],
    clock: list[float],
    mean_signal: list[float],
    peak_signal: list[float],
    *,
    level: float,
    edge: str = "rise",
    start: float | None = None,
    cycles: int,
    mean_tol_pct: float,
    peak_tol_pct: float,
) -> SettledOutcome:
    """The settled verdict, or an outcome carrying ``code``/``error``."""
    from_text = start if start is not None else 0.0
    found = sim_crossings.find_crossings(
        scale, clock, level, edge=edge, start=start, max_count=len(scale)
    )
    if found.total == 0:
        return SettledOutcome(
            code="settled_no_clock_crossing",
            error=(
                f"the clock never crosses {level!r} ({edge}) at or after "
                f"{from_text!r} s -- no cycle to align windows to"
            ),
        )
    edges = found.times
    total = (len(edges) - 1) // cycles
    if total < MIN_WINDOWS:
        return SettledOutcome(
            code="settled_insufficient_windows",
            error=(
                f"only {total} complete window(s) of {cycles} cycle(s) fit "
                f"({len(edges)} {edge} clock edge(s) at or after "
                f"{from_text!r} s); a verdict needs {MIN_WINDOWS} "
                f"(= {MIN_WINDOWS * cycles + 1} edges) -- run the .tran "
                "longer or reduce cycles"
            ),
        )
    windows: list[dict[str, float]] = []
    for k in range(total):
        t_start, t_end = edges[k * cycles], edges[(k + 1) * cycles]
        windows.append(
            {
                "t_start": t_start,
                "t_end": t_end,
                "mean": sim_cycle_mean.time_weighted_mean(
                    scale, mean_signal, t_start, t_end
                ),
                "peak": window_peak(scale, peak_signal, t_start, t_end),
            }
        )
    last, previous = windows[-1], windows[-2]
    mean_delta = relative_delta_pct(last["mean"], previous["mean"])
    peak_delta = relative_delta_pct(last["peak"], previous["peak"])
    return SettledOutcome(
        settled=mean_delta <= mean_tol_pct and peak_delta <= peak_tol_pct,
        mean_delta_pct=mean_delta,
        peak_delta_pct=peak_delta,
        windows_total=total,
        windows=windows[-MAX_REPORTED_WINDOWS:],
    )


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_corner_settleds(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, SettledOutcome]:
    """``{position in measurements_spec: outcome}`` for every settled entry of
    one finished corner. Never raises: any rawfile problem becomes that
    measurement's own ``error``."""
    outcomes: dict[int, SettledOutcome] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not is_settled(spec):
            continue
        outcomes[position] = _extract_one(spec, rawfile_path(corner_dir, index))
        index += 1
    return outcomes


def _extract_one(spec: dict[str, Any], raw_path: str) -> SettledOutcome:
    from .sim import parse_ascii_rawfile

    body = spec["settled"]
    name = spec["name"]
    if not os.path.isfile(raw_path):
        return SettledOutcome(
            code="settled_vector_missing",
            error=(
                f"measurement '{name}': its clock ({body['clock']!r}), mean "
                f"signal ({body['mean']['signal']!r}) or peak signal "
                f"({body['peak']['signal']!r}) did not resolve against this "
                "transient's saved vectors (no rawfile was written) -- check "
                "the node/branch names"
            ),
        )
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError) as exc:
        return SettledOutcome(
            code="settled_vector_missing",
            error=f"measurement '{name}': could not read its settled rawfile: {exc}",
        )
    points = waveform["points"]
    if len(waveform["variables"]) < 4 or not points:
        return SettledOutcome(
            code="settled_vector_missing",
            error=f"measurement '{name}': its vectors yielded no samples",
        )
    outcome = compute_settled(
        [point[0] for point in points],
        [point[1] for point in points],
        [point[2] for point in points],
        [point[3] for point in points],
        level=float(body["level"]),
        edge=body.get("edge", "rise"),
        start=sim_window.window_bound(name, body, "from", form="settled"),
        cycles=body["cycles"],
        mean_tol_pct=float(body["mean"]["tol_pct"]),
        peak_tol_pct=float(body["peak"]["tol_pct"]),
    )
    if outcome.error is not None:
        outcome.error = f"measurement '{name}': {outcome.error}"
    elif any(
        not math.isfinite(window[key])
        for window in outcome.windows
        for key in ("mean", "peak")
    ):
        return SettledOutcome(
            code="settled_vector_missing",
            error=f"measurement '{name}': a windowed signal value is not finite",
        )
    return outcome
