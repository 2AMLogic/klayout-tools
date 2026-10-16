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


def _event_freq(freqs: list[float], ev: _Event) -> float:
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
