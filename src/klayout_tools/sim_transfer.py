"""AC return-ratio (loop-transfer) metrics from sampled numerator/denominator
arrays (issue #2922, first increment of #2724).

This module is a **pure numerical helper**: it consumes a frequency axis and
complex numerator / denominator arrays and returns six scalar stability
metrics. It launches no simulator, reads no files and changes no request or
response schema -- the ``klt sim`` integration lands in a later increment.

Conventions
-----------
``T = numerator / denominator`` per sample. The **negative-feedback
convention** applies: ``T`` is expected to start near 0 degrees at low
frequency, and the phase margin is ``180 + phase(T)`` at the unity crossing.
A bench that produces the opposite sign must negate its numerator *before*
calling :func:`analyze_transfer`; this module never guesses the sign.

Metrics (``None`` plus a ``reasons`` entry when unavailable):

- ``low_frequency_magnitude`` -- linear ``|T|`` of the first *valid* sample.
- ``unity_crossing_hz`` -- frequency of the first *falling* 0 dB crossing.
- ``phase_at_unity_deg`` -- unwrapped phase at that crossing.
- ``phase_margin_deg`` -- ``180 + phase_at_unity_deg``.
- ``gain_margin_db`` -- minus the dB magnitude where the unwrapped phase
  first crosses -180 degrees *downward*.
- ``unity_crossing_count`` -- number of isolated rising plus falling 0 dB
  crossings.

Interpolation
-------------
Between two adjacent valid samples, dB magnitude (``20 log10 |T|``) and the
continuous (unwrapped) phase in degrees are both interpolated **linearly
against log10(frequency)**. Nothing is ever extrapolated: a level reached only
outside the sampled range is not a crossing.

Phase is initialised with the principal argument of the first valid sample
and unwrapped sample to sample (each step wrapped into (-180, 180]). After an
invalid gap the phase **restarts** from the principal argument of the first
valid sample after the gap; no unwrapping is carried across a gap.

Exact hits, touches, plateaus
-----------------------------
Comparisons against the 0 dB / -180 degree levels are exact (``==``).

- A sample that lands exactly on the level is counted once, as a crossing at
  that sample's frequency, only if both neighbours are valid and lie strictly
  on opposite sides; the neighbours give the direction.
- A tangent touch (both neighbours on the same side) is not a crossing.
- An endpoint hit (first/last sample) does not establish a crossing.
- Two or more consecutive valid samples exactly on 0 dB form a **plateau**.
  Its direction and crossing count cannot be known, so every metric that
  depends on 0 dB crossings (``unity_crossing_hz``, ``phase_at_unity_deg``,
  ``phase_margin_deg``, ``unity_crossing_count``) is unavailable with reason
  ``zero_db_plateau``. Likewise a plateau at exactly -180 degrees makes
  ``gain_margin_db`` unavailable with reason ``phase_plateau``.

Invalid samples
---------------
A sample is invalid when the numerator or denominator is nonfinite, the
denominator is zero, or ``|T|`` is zero or nonfinite. Nothing is interpolated
across an invalid sample. Because a gap could hide any number of crossings:

- ``unity_crossing_count`` is unavailable (``invalid_samples``) whenever any
  sample is invalid;
- a first-crossing metric (unity crossing, gain margin) is reported only when
  **no invalid sample precedes the crossing**; otherwise it is unavailable
  (``invalid_samples``) because an earlier crossing might be concealed. A
  valid crossing *before* the first invalid sample is still reported.

A wholly valid sweep with no unity crossing has ``unity_crossing_count == 0``.

Malformed *arrays* (unequal lengths, empty, frequencies that are not finite,
positive and strictly increasing) raise :class:`TransferInputError`.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

#: Reason codes carried in :attr:`TransferMetrics.reasons`.
REASON_NO_VALID_SAMPLES = "no_valid_samples"
REASON_ZERO_DB_PLATEAU = "zero_db_plateau"
REASON_PHASE_PLATEAU = "phase_plateau"
REASON_INVALID_SAMPLES = "invalid_samples"
REASON_NO_FALLING_UNITY_CROSSING = "no_falling_unity_crossing"
REASON_NO_PHASE_CROSSING = "no_downward_phase_crossing"

#: Phase level (degrees) whose downward crossing defines the gain margin.
GAIN_MARGIN_PHASE_DEG = -180.0

_METRIC_NAMES = (
    "low_frequency_magnitude",
    "unity_crossing_hz",
    "phase_at_unity_deg",
    "phase_margin_deg",
    "gain_margin_db",
    "unity_crossing_count",
)


class TransferInputError(ValueError):
    """The frequency / numerator / denominator arrays are malformed."""


@dataclass
class TransferMetrics:
    """The six metrics plus the diagnostics that explain a ``None``."""

    low_frequency_magnitude: float | None = None
    unity_crossing_hz: float | None = None
    phase_at_unity_deg: float | None = None
    phase_margin_deg: float | None = None
    gain_margin_db: float | None = None
    unity_crossing_count: int | None = None
    #: metric name -> reason code, present exactly for the ``None`` metrics.
    reasons: dict[str, str] = field(default_factory=dict)
    #: indices of invalid samples.
    invalid_samples: list[int] = field(default_factory=list)
    #: ``[start, end]`` (inclusive) index pairs of exact 0 dB plateaus.
    zero_db_plateaus: list[list[int]] = field(default_factory=list)
    #: ``[start, end]`` (inclusive) index pairs of exact -180 degree plateaus.
    phase_plateaus: list[list[int]] = field(default_factory=list)
    #: per-sample dB magnitude, ``None`` for invalid samples.
    magnitude_db: list[float | None] = field(default_factory=list)
    #: per-sample unwrapped phase (degrees), ``None`` for invalid samples.
    phase_unwrapped_deg: list[float | None] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (no NaN/Infinity can occur by construction)."""
        out: dict[str, Any] = {name: getattr(self, name) for name in _METRIC_NAMES}
        out["reasons"] = dict(self.reasons)
        out["invalid_samples"] = list(self.invalid_samples)
        out["zero_db_plateaus"] = [list(p) for p in self.zero_db_plateaus]
        out["phase_plateaus"] = [list(p) for p in self.phase_plateaus]
        out["magnitude_db"] = list(self.magnitude_db)
        out["phase_unwrapped_deg"] = list(self.phase_unwrapped_deg)
        return out


@dataclass(frozen=True)
class _Event:
    """A level crossing between samples ``index`` and ``index + 1``.

    ``weight`` is the fractional position along that segment (0 means exactly
    at sample ``index``, the isolated-exact-hit case).
    """

    index: int
    weight: float
    direction: str  # "rise" | "fall"


def _wrap_deg(delta: float) -> float:
    """Wrap a phase step into (-180, 180]."""
    w = math.fmod(delta, 360.0)
    if w > 180.0:
        w -= 360.0
    elif w <= -180.0:
        w += 360.0
    return w


def _check_arrays(
    freq_hz: Sequence[float],
    numerator: Sequence[complex],
    denominator: Sequence[complex],
) -> tuple[list[float], list[complex], list[complex]]:
    try:
        freqs = [float(f) for f in freq_hz]
        nums = [complex(v) for v in numerator]
        dens = [complex(v) for v in denominator]
    except (TypeError, ValueError) as exc:
        raise TransferInputError(f"non-numeric array element: {exc}") from exc
    if not (len(freqs) == len(nums) == len(dens)):
        raise TransferInputError(
            "arrays must have equal length "
            f"(freq={len(freqs)}, numerator={len(nums)}, denominator={len(dens)})"
        )
    if not freqs:
        raise TransferInputError("arrays must contain at least one sample")
    for i, f in enumerate(freqs):
        if not math.isfinite(f) or f <= 0.0:
            raise TransferInputError(
                f"frequency[{i}]={f!r} must be finite and positive"
            )
        if i and f <= freqs[i - 1]:
            raise TransferInputError(
                f"frequencies must be strictly increasing (index {i})"
            )
    return freqs, nums, dens


def _ratio(num: complex, den: complex) -> complex | None:
    """``num / den`` or ``None`` when the sample is invalid."""
    if not (cmath.isfinite(num) and cmath.isfinite(den)):
        return None
    if den == 0:
        return None
    try:
        t = num / den
        mag = abs(t)
    except (ZeroDivisionError, OverflowError):
        return None
    if not cmath.isfinite(t) or not math.isfinite(mag) or mag == 0.0:
        return None
    return t


def _plateaus(values: list[float | None], level: float) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    n = len(values)
    i = 0
    while i < n:
        if values[i] is not None and values[i] == level:
            j = i
            while j + 1 < n and values[j + 1] is not None and values[j + 1] == level:
                j += 1
            if j > i:
                out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _exact_hit_event(values: list[float | None], k: int, level: float) -> _Event | None:
    """Event for a sample exactly on ``level`` whose neighbours straddle it."""
    if k == 0 or k + 1 >= len(values):
        return None
    left, right = values[k - 1], values[k + 1]
    if left is None or right is None:
        return None
    return _straddle_event(k, left - level, right - level, 0.0)


def _straddle_event(index: int, a: float, b: float, weight: float) -> _Event | None:
    if a < 0.0 < b:
        return _Event(index, weight, "rise")
    if a > 0.0 > b:
        return _Event(index, weight, "fall")
    return None


def _segment_event(values: list[float | None], k: int, level: float) -> _Event | None:
    """Event for a strict crossing between samples ``k`` and ``k + 1``."""
    v = values[k]
    nxt = values[k + 1] if k + 1 < len(values) else None
    if v is None or nxt is None:
        return None
    a, b = v - level, nxt - level
    if not (a < 0.0 < b or a > 0.0 > b):
        return None
    return _straddle_event(k, a, b, (0.0 - a) / (b - a))


def _level_events(values: list[float | None], level: float) -> list[_Event]:
    """Ordered isolated crossings of ``level`` (see module docstring)."""
    events: list[_Event] = []
    for k, v in enumerate(values):
        if v is None:
            continue
        ev = (
            _exact_hit_event(values, k, level)
            if v == level
            else _segment_event(values, k, level)
        )
        if ev is not None:
            events.append(ev)
    return events


def _lerp(a: float, b: float, w: float) -> float:
    return a if w == 0.0 else a + w * (b - a)


def _event_freq(freqs: Sequence[float], ev: _Event) -> float:
    if ev.weight == 0.0:
        return freqs[ev.index]
    lo, hi = math.log10(freqs[ev.index]), math.log10(freqs[ev.index + 1])
    return 10.0 ** _lerp(lo, hi, ev.weight)


def _at(values: list[float | None], ev: _Event) -> float:
    a = values[ev.index]
    assert a is not None
    if ev.weight == 0.0:
        return a
    b = values[ev.index + 1]
    assert b is not None
    return _lerp(a, b, ev.weight)


def _unwrap_phase(ratios: list[complex | None]) -> list[float | None]:
    """Unwrapped phase, restarting from the principal argument after a gap."""
    phase: list[float | None] = [None] * len(ratios)
    prev_principal: float | None = None
    for i, t in enumerate(ratios):
        if t is None:
            prev_principal = None
            continue
        principal = math.degrees(cmath.phase(t))
        if prev_principal is None:
            phase[i] = principal
        else:
            prev_phase = phase[i - 1]
            assert prev_phase is not None
            phase[i] = prev_phase + _wrap_deg(principal - prev_principal)
        prev_principal = principal
    return phase


def _fill_unity_metrics(
    res: TransferMetrics,
    freqs: list[float],
    mag_db: list[float | None],
    phase: list[float | None],
    invalid: list[int],
) -> None:
    """Unity (0 dB) crossing count, frequency, phase and phase margin."""
    mag_plateaus = _plateaus(mag_db, 0.0)
    res.zero_db_plateaus = [list(p) for p in mag_plateaus]
    unity_events = _level_events(mag_db, 0.0)

    if mag_plateaus:
        for name in (
            "unity_crossing_hz",
            "phase_at_unity_deg",
            "phase_margin_deg",
            "unity_crossing_count",
        ):
            res.reasons[name] = REASON_ZERO_DB_PLATEAU
        return

    if invalid:
        res.reasons["unity_crossing_count"] = REASON_INVALID_SAMPLES
    else:
        res.unity_crossing_count = len(unity_events)

    first_fall = next((e for e in unity_events if e.direction == "fall"), None)
    if first_fall is not None and not any(j < first_fall.index for j in invalid):
        res.unity_crossing_hz = _event_freq(freqs, first_fall)
        phase_u = _at(phase, first_fall)
        res.phase_at_unity_deg = phase_u
        res.phase_margin_deg = 180.0 + phase_u
    else:
        reason = REASON_INVALID_SAMPLES if invalid else REASON_NO_FALLING_UNITY_CROSSING
        for name in ("unity_crossing_hz", "phase_at_unity_deg", "phase_margin_deg"):
            res.reasons[name] = reason


def _fill_gain_margin(
    res: TransferMetrics,
    mag_db: list[float | None],
    phase: list[float | None],
    invalid: list[int],
) -> None:
    """Gain margin at the first downward -180 degree phase crossing."""
    phase_plateaus = _plateaus(phase, GAIN_MARGIN_PHASE_DEG)
    res.phase_plateaus = [list(p) for p in phase_plateaus]
    if phase_plateaus:
        res.reasons["gain_margin_db"] = REASON_PHASE_PLATEAU
        return
    first_down = next(
        (
            e
            for e in _level_events(phase, GAIN_MARGIN_PHASE_DEG)
            if e.direction == "fall"
        ),
        None,
    )
    if first_down is not None and not any(j < first_down.index for j in invalid):
        res.gain_margin_db = -_at(mag_db, first_down)
    else:
        res.reasons["gain_margin_db"] = (
            REASON_INVALID_SAMPLES if invalid else REASON_NO_PHASE_CROSSING
        )


def analyze_transfer(
    freq_hz: Sequence[float],
    numerator: Sequence[complex],
    denominator: Sequence[complex],
) -> TransferMetrics:
    """Compute the six return-ratio metrics for ``T = numerator / denominator``.

    Raises :class:`TransferInputError` for malformed arrays; invalid *samples*
    never raise -- they surface as ``None`` metrics with reasons.
    """
    freqs, nums, dens = _check_arrays(freq_hz, numerator, denominator)

    ratios = [_ratio(n_, d_) for n_, d_ in zip(nums, dens, strict=False)]
    invalid = [i for i, t in enumerate(ratios) if t is None]
    mag_db: list[float | None] = [
        None if t is None else 20.0 * math.log10(abs(t)) for t in ratios
    ]
    phase = _unwrap_phase(ratios)

    res = TransferMetrics(
        invalid_samples=invalid,
        magnitude_db=mag_db,
        phase_unwrapped_deg=phase,
    )

    first_ratio = next((t for t in ratios if t is not None), None)
    if first_ratio is None:
        for name in _METRIC_NAMES:
            res.reasons[name] = REASON_NO_VALID_SAMPLES
        return res
    res.low_frequency_magnitude = abs(first_ratio)

    _fill_unity_metrics(res, freqs, mag_db, phase, invalid)
    _fill_gain_margin(res, mag_db, phase, invalid)
    return res


# ---------------------------------------------------------------------------
# All-crossing margins, encirclement and band validity (issue #3088)
# ---------------------------------------------------------------------------
#
# ``analyze_margins`` is additive: it does not change ``analyze_transfer`` or
# its first-crossing metrics. Where those use the *unwrapped* phase, this
# reducer never does -- phase is only ever compared **modulo 360 degrees**,
# using the wrapped step between two adjacent samples, so a phase that has
# wandered many turns away cannot turn a benign crossing into a large
# negative margin.
#
# Conventions (same negative-feedback sign convention as above):
#
# - ``phase_margin_deg`` at a 0 dB crossing is ``wrap(phase + 180)`` into
#   (-180, 180]: the signed distance to the *nearest* -180 degree line,
#   positive when the phase is short of it. It is evaluated at every 0 dB
#   crossing, rising and falling; ``phase_margin_min_deg`` is the minimum.
# - ``gain_margin_db`` at a -180 degree crossing (mod 360, either direction)
#   is minus the dB magnitude there: positive when ``|T| < 1``, negative when
#   ``|T| > 1``. ``gain_margin_min_db`` is the minimum.
# - ``encirclement`` counts clockwise encirclements of -1 by the Nyquist
#   curve (both halves of the contour) as ``2 * net`` crossings of the ray
#   (-inf, -1): a crossing with Im(T) going negative -> positive is +1, the
#   reverse is -1. For an open-loop-stable loop, 0 means stable and ``N > 0``
#   means ``N`` right-half-plane closed-loop poles. Bode data cannot reveal
#   open-loop unstable poles; the count is reported, not interpreted.
#   It is ``None`` (``indeterminate``) unless the band supports it.

REASON_NO_ZERO_DB_CROSSING = "no_zero_db_crossing"
REASON_NO_PHASE_CROSSING_ANY = "no_phase_crossing"
REASON_TOP_NOT_BELOW_UNITY = "top_not_below_unity"
REASON_CROSSING_NOT_BRACKETED = "crossing_not_bracketed"
REASON_LOW_FREQUENCY_ON_AXIS = "low_frequency_on_negative_real_axis"

#: Default resolution gates for a crossing to count as bracketed.
DEFAULT_MIN_SAMPLES_PER_DECADE = 10.0
DEFAULT_MAX_PHASE_STEP_DEG = 90.0


def wrap_phase_margin_deg(phase_deg: float) -> float:
    """Signed distance (degrees) from ``phase_deg`` to the nearest -180 line."""
    return _wrap_deg(phase_deg + 180.0)


@dataclass
class MarginReport:
    """All-crossing margins, encirclement and band validity for one trace."""

    #: ``{freq_hz, direction, phase_margin_deg}`` for every 0 dB crossing.
    phase_margins: list[dict[str, Any]] = field(default_factory=list)
    #: ``{freq_hz, direction, gain_margin_db}`` for every -180 degree crossing.
    gain_margins: list[dict[str, Any]] = field(default_factory=list)
    phase_margin_min_deg: float | None = None
    gain_margin_min_db: float | None = None
    #: Clockwise encirclements of -1, or ``None`` when indeterminate.
    encirclements: int | None = None
    band: dict[str, Any] = field(default_factory=dict)
    #: metric name -> reason code, present for each ``None`` metric.
    reasons: dict[str, str] = field(default_factory=dict)
    #: reasons the band/resolution cannot support a pass verdict.
    ambiguity: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (never NaN/Infinity)."""
        return {
            "phase_margins": [dict(p) for p in self.phase_margins],
            "gain_margins": [dict(g) for g in self.gain_margins],
            "phase_margin_min_deg": self.phase_margin_min_deg,
            "gain_margin_min_db": self.gain_margin_min_db,
            "encirclement": (
                "indeterminate" if self.encirclements is None else self.encirclements
            ),
            "band": dict(self.band),
            "reasons": dict(self.reasons),
            "ambiguity": list(self.ambiguity),
        }


def _add(items: list[str], reason: str) -> None:
    if reason not in items:
        items.append(reason)


def _seg_step(principal: Sequence[float | None], k: int) -> float | None:
    """Wrapped phase step (degrees) across segment ``k``; ``None`` at a gap."""
    a, b = principal[k], principal[k + 1]
    if a is None or b is None:
        return None
    return _wrap_deg(b - a)


class _Bracketing:
    """Tracks whether every crossing sits on a resolved sample segment."""

    def __init__(
        self,
        freqs: Sequence[float],
        principal: Sequence[float | None],
        min_samples_per_decade: float,
        max_phase_step_deg: float,
    ) -> None:
        self.freqs = freqs
        self.principal = principal
        self.min_samples_per_decade = min_samples_per_decade
        self.max_phase_step_deg = max_phase_step_deg
        self.bracketed = True
        self.densities: list[float] = []

    def check(self, ev: _Event) -> None:
        k = ev.index
        segments = [k] if ev.weight != 0.0 else [s for s in (k - 1, k) if s >= 0]
        for s in segments:
            if s + 1 >= len(self.freqs):
                continue
            d = 1.0 / math.log10(self.freqs[s + 1] / self.freqs[s])
            self.densities.append(d)
            step = _seg_step(self.principal, s)
            if d < self.min_samples_per_decade or (
                step is not None and abs(step) > self.max_phase_step_deg
            ):
                self.bracketed = False


def _phase_margin_entries(
    freqs: Sequence[float],
    principal: Sequence[float | None],
    events: Sequence[_Event],
    bracket: _Bracketing,
) -> list[dict[str, Any]]:
    """Phase margin at every 0 dB crossing."""
    out: list[dict[str, Any]] = []
    for ev in events:
        bracket.check(ev)
        base = principal[ev.index]
        if ev.weight == 0.0:
            phi = base
        else:
            step = _seg_step(principal, ev.index)
            phi = None if step is None or base is None else base + ev.weight * step
        assert phi is not None
        out.append(
            {
                "freq_hz": _event_freq(freqs, ev),
                "direction": ev.direction,
                "phase_margin_deg": wrap_phase_margin_deg(phi),
            }
        )
    return out


_GainEvent = tuple[float, str, float, int]  # freq, direction, dB, sample index


def _on_line_crossing(
    k: int,
    freqs: Sequence[float],
    mag_db: Sequence[float | None],
    principal: Sequence[float | None],
    bracket: _Bracketing,
) -> _GainEvent | None:
    """-180 crossing landing exactly on sample ``k``, if it is a true crossing."""
    if k == 0 or k + 1 >= len(freqs):
        return None
    left, right = _seg_step(principal, k - 1), _seg_step(principal, k)
    if left is None or right is None or left * right <= 0.0:
        # Tangent touch (or a gap): not a crossing.
        return None
    db = mag_db[k]
    assert db is not None
    bracket.check(_Event(k, 0.0, "rise"))
    return (freqs[k], "up" if right > 0.0 else "down", db, k)


def _segment_crossing(
    k: int,
    gk: float,
    freqs: Sequence[float],
    mag_db: Sequence[float | None],
    g: Sequence[float | None],
    principal: Sequence[float | None],
    bracket: _Bracketing,
) -> _GainEvent | None:
    """-180 crossing strictly inside segment ``k``, if there is one."""
    if k + 1 >= len(freqs):
        return None
    step = _seg_step(principal, k)
    if step is None:
        return None
    end = gk + step if g[k + 1] != 0.0 else 0.0
    if gk * end >= 0.0:
        return None
    w = -gk / step
    bracket.check(_Event(k, w, "rise"))
    da, db_ = mag_db[k], mag_db[k + 1]
    assert da is not None and db_ is not None
    lo, hi = math.log10(freqs[k]), math.log10(freqs[k + 1])
    return (
        10.0 ** _lerp(lo, hi, w),
        "up" if step > 0.0 else "down",
        _lerp(da, db_, w),
        k,
    )


def _gain_crossings(
    freqs: Sequence[float],
    mag_db: Sequence[float | None],
    g: Sequence[float | None],
    principal: Sequence[float | None],
    bracket: _Bracketing,
) -> list[_GainEvent]:
    """Every -180 degree crossing (mod 360, either direction)."""
    events: list[_GainEvent] = []
    for k, gk in enumerate(g):
        if gk is None:
            continue
        if gk == 0.0:
            ev = _on_line_crossing(k, freqs, mag_db, principal, bracket)
        else:
            ev = _segment_crossing(k, gk, freqs, mag_db, g, principal, bracket)
        if ev is not None:
            events.append(ev)
    return events


def _encirclement_net(gain_events: Sequence[_GainEvent]) -> int:
    """Net clockwise crossings of the ray left of -1.

    Phase falling through -180 sends Im(T) from - to + left of -1, which is
    clockwise around -1; rising is counter-clockwise.
    """
    net = 0
    for _f, direction, db, _k in gain_events:
        if db > 0.0:
            net += 1 if direction == "down" else -1
    return net


def _fill_extrema(rep: MarginReport, mag_plateaus: bool, phase_plateaus: bool) -> None:
    """Set the minimum margins, or the reason each one is unavailable."""
    if rep.phase_margins:
        rep.phase_margin_min_deg = min(p["phase_margin_deg"] for p in rep.phase_margins)
    elif not mag_plateaus:
        rep.reasons["phase_margin_min_deg"] = REASON_NO_ZERO_DB_CROSSING
    else:
        rep.reasons["phase_margin_min_deg"] = REASON_ZERO_DB_PLATEAU
    if rep.gain_margins:
        rep.gain_margin_min_db = min(m["gain_margin_db"] for m in rep.gain_margins)
    else:
        rep.reasons["gain_margin_min_db"] = (
            REASON_PHASE_PLATEAU if phase_plateaus else REASON_NO_PHASE_CROSSING_ANY
        )


def _fill_band(
    rep: MarginReport,
    freqs: Sequence[float],
    ratios: Sequence[complex | None],
    mag_db: Sequence[float | None],
    g: Sequence[float | None],
    bracket: _Bracketing,
) -> None:
    """Band validity record plus the ambiguity reasons it implies."""
    amb = rep.ambiguity
    first = next((i for i, t in enumerate(ratios) if t is not None), None)
    last = ratios[-1]
    top_ok = last is not None and abs(last) < 1.0
    densities = bracket.densities
    rep.band = {
        "f_min_hz": freqs[0],
        "f_max_hz": freqs[-1],
        "mag_below_unity_at_top": top_ok,
        "low_freq_mag_db": None if first is None else mag_db[first],
        "min_samples_per_decade_near_crossing": min(densities) if densities else None,
        "crossings_bracketed": bracket.bracketed,
    }
    if first is None:
        _add(amb, REASON_NO_VALID_SAMPLES)
    if any(t is None for t in ratios):
        _add(amb, REASON_INVALID_SAMPLES)
    if not top_ok:
        _add(amb, REASON_TOP_NOT_BELOW_UNITY)
    if not bracket.bracketed:
        _add(amb, REASON_CROSSING_NOT_BRACKETED)
    # The low-frequency start must not sit on the negative real axis beyond -1
    # (a type-2+ integrator loop, whose indentation arc crosses that ray; not
    # counted here).
    first_offset = g[first] if first is not None else None
    if (
        first is not None
        and (mag_db[first] or 0.0) > 0.0
        and abs(180.0 if first_offset is None else first_offset) < 45.0
    ):
        _add(amb, REASON_LOW_FREQUENCY_ON_AXIS)


def analyze_margins(
    freq_hz: Sequence[float],
    numerator: Sequence[complex],
    denominator: Sequence[complex],
    *,
    min_samples_per_decade: float = DEFAULT_MIN_SAMPLES_PER_DECADE,
    max_phase_step_deg: float = DEFAULT_MAX_PHASE_STEP_DEG,
) -> MarginReport:
    """Reduce ``T = numerator / denominator`` to every-crossing margins.

    A crossing is *bracketed* when its sample segment has at least
    ``min_samples_per_decade`` samples per decade and a wrapped phase step of
    at most ``max_phase_step_deg``; otherwise the verdict is ambiguous, because
    a segment that coarse could hide further crossings. Any invalid sample is
    also ambiguous (a gap could hide any number of crossings). Crossings that
    *were* found are always reported. Malformed arrays raise
    :class:`TransferInputError`.
    """
    freqs, nums, dens = _check_arrays(freq_hz, numerator, denominator)
    ratios = [_ratio(n_, d_) for n_, d_ in zip(nums, dens, strict=False)]
    mag_db: list[float | None] = [
        None if t is None else 20.0 * math.log10(abs(t)) for t in ratios
    ]
    principal: list[float | None] = [
        None if t is None else math.degrees(cmath.phase(t)) for t in ratios
    ]
    rep = MarginReport()
    bracket = _Bracketing(freqs, principal, min_samples_per_decade, max_phase_step_deg)

    # Phase relative to the -180 line, locally continuous across each segment.
    # Offsets within 1e-9 degree of the line are snapped onto it: ``atan2``
    # noise must not turn a hit into a miss at the branch cut.
    g: list[float | None] = [
        None
        if p is None
        else (0.0 if abs(_wrap_deg(p + 180.0)) < 1e-9 else _wrap_deg(p + 180.0))
        for p in principal
    ]

    # --- 0 dB crossings -> phase margins ---------------------------------
    mag_plateaus = bool(_plateaus(mag_db, 0.0))
    if mag_plateaus:
        _add(rep.ambiguity, REASON_ZERO_DB_PLATEAU)
    else:
        unity_events = _level_events(mag_db, 0.0)
        rep.phase_margins = _phase_margin_entries(
            freqs, principal, unity_events, bracket
        )

    # --- -180 degree crossings -> gain margins + encirclement -------------
    phase_plateaus = bool(_plateaus(g, 0.0))
    gain_events: list[_GainEvent] = []
    if phase_plateaus:
        _add(rep.ambiguity, REASON_PHASE_PLATEAU)
    else:
        gain_events = _gain_crossings(freqs, mag_db, g, principal, bracket)
    rep.gain_margins = [
        {"freq_hz": f, "direction": d, "gain_margin_db": -db}
        for f, d, db, _k in gain_events
    ]

    _fill_extrema(rep, mag_plateaus, phase_plateaus)
    _fill_band(rep, freqs, ratios, mag_db, g, bracket)

    # Encirclement is decidable only when the contour closes cleanly: |T| < 1
    # at the top, no gaps, no plateaus, and the low-frequency start is not on
    # the negative real axis beyond -1.
    if rep.ambiguity:
        rep.reasons["encirclement"] = rep.ambiguity[0]
    else:
        rep.encirclements = 2 * _encirclement_net(gain_events)
    return rep


def grade_margins(
    report: MarginReport,
    *,
    min_phase_margin_deg: float | None = None,
    min_gain_margin_db: float | None = None,
    require_no_encirclement: bool = False,
) -> str:
    """Grade a :class:`MarginReport` as ``"pass"``, ``"fail"`` or ``"ambiguous"``.

    A violation among crossings that were actually *found* is a sound
    ``fail`` even when the band is shaky (a hidden crossing can only lower the
    minimum). ``pass`` requires a clean band; otherwise the outcome is
    ``ambiguous`` -- never ``pass`` and never ``fail``. A requested limit whose
    metric is unavailable because no crossing exists (``no_phase_crossing`` /
    ``no_zero_db_crossing``) is vacuously satisfied on a clean band.
    """
    if (
        min_phase_margin_deg is not None
        and report.phase_margin_min_deg is not None
        and report.phase_margin_min_deg < min_phase_margin_deg
    ):
        return "fail"
    if (
        min_gain_margin_db is not None
        and report.gain_margin_min_db is not None
        and report.gain_margin_min_db < min_gain_margin_db
    ):
        return "fail"
    if require_no_encirclement and report.encirclements:
        return "fail"
    if report.ambiguity:
        return "ambiguous"
    return "pass"
