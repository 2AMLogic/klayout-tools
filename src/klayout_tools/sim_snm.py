"""Butterfly static noise margin (SNM) numerics for ``klt sim`` (issue #3895).

Pure Python (no numpy). Given one swept axis ``x`` and the two voltage-transfer
curves ``y1(x)`` and ``y2(x)`` of a bistable pair, the butterfly is::

    C1 = {(x, y1(x))}        C2 = {(y2(x), x)}   (the second VTC, mirrored)

The static noise margin is the side of the largest axis-parallel square that
fits inside the *smaller* of the two eyes the curves enclose. A square whose
corners touch both curves has its diagonal along the ``(1, 1)`` direction, so
after rotating by 45 degrees (``u = (x - y)/sqrt(2)``, ``v = (x + y)/sqrt(2)``)
the diagonal is the gap ``v1(u) - v2(u)`` at one ``u``: the per-eye extreme of
that gap, divided by ``sqrt(2)``, is the square's side (Seevinck's method).
Both rotated curves are polylines in ``u``, so the gap is piecewise linear and
its extremes sit at the knots of either curve -- the result is exact for the
sampled polylines, not a resampled approximation.

Not the vertical-gap shortcut (``max |y1 - C2(x)|``), which reads the chord
along ``y`` rather than the square's diagonal and over-reads the margin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_SQRT2 = math.sqrt(2.0)

#: Failure reasons -- the leading ``[snm_*]`` token of the diagnostic.
REASON_INVALID = "snm_invalid_data"
REASON_NON_MONOTONIC = "snm_non_monotonic"
REASON_NO_SECOND_EYE = "snm_no_second_eye"


class SnmInputError(ValueError):
    """The curves cannot support a butterfly SNM; ``reason`` is the token."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SnmResult:
    """``snm`` is ``min(eye_upper_left, eye_lower_right)``, in the units of
    the input axes. ``eye_*`` are the two per-eye inscribed-square sides."""

    snm: float
    eye_upper_left: float
    eye_lower_right: float

    def to_dict(self) -> dict[str, float]:
        return {
            "eye_upper_left": self.eye_upper_left,
            "eye_lower_right": self.eye_lower_right,
        }


def _strictly_monotonic(values: list[float]) -> int:
    """``+1`` strictly increasing, ``-1`` strictly decreasing, ``0`` neither."""
    if all(b > a for a, b in zip(values, values[1:], strict=False)):
        return 1
    if all(b < a for a, b in zip(values, values[1:], strict=False)):
        return -1
    return 0


def _rotate(
    points: list[tuple[float, float]], name: str
) -> tuple[list[float], list[float]]:
    """``(u, v)`` columns of the rotated polyline, ``u`` strictly increasing.

    A voltage-transfer curve is non-increasing, so ``u = (x - y)/sqrt(2)`` is
    strictly monotonic along it; a curve that is not (an overshoot, a
    non-inverting stretch) has no single-valued rotated form and is refused
    rather than reduced to a number nobody can interpret."""
    u = [(px - py) / _SQRT2 for px, py in points]
    v = [(px + py) / _SQRT2 for px, py in points]
    direction = _strictly_monotonic(u)
    if direction == 0:
        raise SnmInputError(
            REASON_NON_MONOTONIC,
            f"curve {name} is not a non-increasing transfer curve (its "
            "45-degree rotation is not single-valued)",
        )
    if direction < 0:
        u.reverse()
        v.reverse()
    return u, v


def _interp(u: list[float], v: list[float], at: float) -> float:
    """Linear interpolation of the polyline ``(u, v)`` at ``at`` (inside its
    range)."""
    lo, hi = 0, len(u) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if u[mid] <= at:
            lo = mid
        else:
            hi = mid
    span = u[hi] - u[lo]
    return v[lo] + (v[hi] - v[lo]) * ((at - u[lo]) / span)


def analyze_snm(x: list[float], y1: list[float], y2: list[float]) -> SnmResult:
    """Butterfly SNM of the pair ``(x, y1(x))`` and ``(y2(x), x)``.

    Raises :class:`SnmInputError` for mismatched/short/non-finite input, an
    ``x`` axis that is not strictly monotonic, a curve that is not
    non-increasing, curves whose rotated ranges do not overlap, or a butterfly
    with fewer than two eyes (the curves never cross, or one never leaves the
    other)."""
    if not (len(x) == len(y1) == len(y2)):
        raise SnmInputError(REASON_INVALID, "x, y1 and y2 differ in length")
    if len(x) < 3:
        raise SnmInputError(REASON_INVALID, "needs at least three sweep points")
    if not all(math.isfinite(s) for s in (*x, *y1, *y2)):
        raise SnmInputError(REASON_INVALID, "contains non-finite samples")
    if _strictly_monotonic(x) == 0:
        raise SnmInputError(
            REASON_NON_MONOTONIC, "the swept x axis is not strictly monotonic"
        )
    u1, v1 = _rotate(list(zip(x, y1, strict=True)), "y1")
    u2, v2 = _rotate(list(zip(y2, x, strict=True)), "y2")
    lo, hi = max(u1[0], u2[0]), min(u1[-1], u2[-1])
    if not lo < hi:
        raise SnmInputError(
            REASON_NO_SECOND_EYE, "the two curves share no rotated range"
        )
    knots = sorted(
        {lo, hi, *(k for k in u1 if lo <= k <= hi), *(k for k in u2 if lo <= k <= hi)}
    )
    gaps = [_interp(u1, v1, k) - _interp(u2, v2, k) for k in knots]
    # `v` runs along (1, 1): C1 above C2 (positive gap) is the upper-left eye.
    upper_left = max(max(gaps), 0.0) / _SQRT2
    lower_right = max(-min(gaps), 0.0) / _SQRT2
    if upper_left <= 0.0 or lower_right <= 0.0:
        raise SnmInputError(
            REASON_NO_SECOND_EYE,
            "the curves enclose fewer than two eyes (not a butterfly)",
        )
    return SnmResult(min(upper_left, lower_right), upper_left, lower_right)
