"""Independent oracles for the butterfly SNM tests (issue #3895).

``tanh_inverter`` is a smooth, symmetric voltage-transfer curve with an
analytic inverse, so the largest inscribed square can be found by a direct
geometric search that shares no code with the 45-degree rotation under test.
"""

from __future__ import annotations

import math


def tanh_inverter(x: float, gain: float = 5.0) -> float:
    return 0.5 * (1.0 - math.tanh(gain * (x - 0.5)))


def tanh_inverter_inverse(y: float, gain: float = 5.0) -> float:
    return 0.5 + math.atanh(1.0 - 2.0 * y) / gain


def brute_force_eye(gain: float = 5.0) -> float:
    """Side of the largest square in the upper-left eye of the symmetric
    butterfly ``y = f(x)`` / ``x = f(y)``.

    Both boundaries are decreasing, so a square ``[x0, x0+s] x [y0, y0+s]``
    fits iff its bottom-left corner is above the mirrored curve
    (``y0 >= f^-1(x0)``) and its top-right corner is below ``f``
    (``y0 + s <= f(x0 + s)``). Bisect ``s`` on the tightest ``y0``."""

    def fits(side: float) -> bool:
        for i in range(1, 4000):
            x0 = i / 4000.0
            if x0 + side > 1.0:
                break
            y0 = tanh_inverter_inverse(x0, gain)
            if y0 + side <= tanh_inverter(x0 + side, gain):
                return True
        return False

    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if fits(mid) else (lo, mid)
    return lo


def vertical_gap_shortcut(x: list[float], y1: list[float], y2: list[float]) -> float:
    """The error-prone shortcut the issue replaces: the largest *vertical*
    distance between ``y1(x)`` and the mirrored second curve resampled onto
    the same ``x`` axis, minimum over the two eyes. It reads a chord along
    ``y`` instead of the square's diagonal and over-reads the margin."""
    order = sorted(range(len(x)), key=lambda i: y2[i])
    cx = [y2[i] for i in order]  # C2 abscissa
    cy = [x[i] for i in order]  # C2 ordinate

    def c2(at: float) -> float:
        for k in range(len(cx) - 1):
            if cx[k] <= at <= cx[k + 1]:
                t = (at - cx[k]) / (cx[k + 1] - cx[k])
                return cy[k] + t * (cy[k + 1] - cy[k])
        return cy[0] if at < cx[0] else cy[-1]

    gaps = [a - c2(px) for px, a in zip(x, y1, strict=True)]
    return min(max(gaps), -min(gaps))
