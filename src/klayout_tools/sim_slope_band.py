"""Local-slope-band run search over a swept curve (issue #5518).

The numerics behind ``measurements[].slope_band``: given a ``dc`` sweep of
``y`` against ``x``, find the largest contiguous run of sweep points
containing a named anchor over which the local slope ``dy/dx`` stays inside a
closed band, and reduce it to six scalars. "Usable range", "compliance range"
and "linear region" spec rows are all this shape.

Deliberately a pure function over two float lists -- no simulator, no
filesystem, no request dicts -- so the reduction can be tested directly.
:mod:`klayout_tools.sim_slope_band_meas` is the ``klt sim`` wiring around it
(request validation, deck emission, rawfile read-back, grading).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: Reason tokens a failed run search raises, echoed into the measurement's
#: ``[<reason>]`` diagnostic by the measurement module.
REASON_INVALID = "slope_band_invalid_data"
REASON_NON_MONOTONIC = "slope_band_non_monotonic"
REASON_ANCHOR_OUTSIDE = "slope_band_anchor_outside_sweep"
REASON_ANCHOR_OUT_OF_BAND = "slope_band_anchor_out_of_band"

#: The scalars a ``slope_band`` entry's ``report`` may select, all derived
#: from the same run.
REPORTS = ("y_span", "y_lo", "y_hi", "x_span", "x_lo", "x_hi")


class SlopeBandInputError(ValueError):
    """The sweep cannot support a run search. ``reason`` is the token the
    measurement's diagnostic message leads with."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SlopeBandResult:
    """One run search's outcome: the six scalars, where each edge stopped and
    how many sweep points the run spans."""

    y_lo: float
    y_hi: float
    y_span: float
    x_lo: float
    x_hi: float
    x_span: float
    edge_lo_at: str
    edge_hi_at: str
    points_in_run: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "y_lo": self.y_lo,
            "y_hi": self.y_hi,
            "y_span": self.y_span,
            "x_lo": self.x_lo,
            "x_hi": self.x_hi,
            "x_span": self.x_span,
            "edge_lo_at": self.edge_lo_at,
            "edge_hi_at": self.edge_hi_at,
            "points_in_run": self.points_in_run,
        }

    def select(self, report: str) -> float:
        """The one scalar ``report`` names -- the entry's graded ``value``."""
        return float(getattr(self, report))


def local_slopes(xs: list[float], ys: list[float]) -> list[float]:
    """``dy/dx`` at each sweep point by central difference over the solver's
    own points -- one-sided at the first and last point.

    No interpolation and no smoothing: the slope at point ``i`` is
    ``(y[i+1] - y[i-1]) / (x[i+1] - x[i-1])``, the symmetric estimate on the
    solver's grid. ``xs`` must already be strictly increasing, so no
    denominator here can be zero.
    """
    n = len(xs)
    slopes = [(ys[1] - ys[0]) / (xs[1] - xs[0])]
    slopes.extend(
        (ys[i + 1] - ys[i - 1]) / (xs[i + 1] - xs[i - 1]) for i in range(1, n - 1)
    )
    slopes.append((ys[n - 1] - ys[n - 2]) / (xs[n - 1] - xs[n - 2]))
    return slopes


def _ascending(xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
    """``(xs, ys)`` with ``xs`` strictly increasing, reversing a descending
    sweep so a ``dc`` run written high-to-low gives the identical answer.

    Reversing flips the sign of both differences in every slope estimate, so
    the slopes -- and therefore the run -- are unchanged.
    """
    if all(b > a for a, b in zip(xs, xs[1:], strict=False)):
        return xs, ys
    if all(b < a for a, b in zip(xs, xs[1:], strict=False)):
        return xs[::-1], ys[::-1]
    raise SlopeBandInputError(
        REASON_NON_MONOTONIC,
        "its x vector is not strictly monotonic (a repeated or non-ordered "
        "sweep value makes the local slope undefined)",
    )


def _validate_sweep(xs: list[float], ys: list[float]) -> None:
    if len(xs) != len(ys):
        raise SlopeBandInputError(
            REASON_INVALID,
            f"its x and y vectors have different lengths ({len(xs)} vs {len(ys)})",
        )
    if len(xs) < 3:
        raise SlopeBandInputError(
            REASON_INVALID,
            f"a central-difference slope needs at least three sweep points "
            f"(got {len(xs)}) -- increase the dc sweep's resolution",
        )
    if not all(math.isfinite(v) for v in (*xs, *ys)):
        raise SlopeBandInputError(
            REASON_INVALID, "its sweep contains a non-finite (nan/inf) sample"
        )


def analyze_slope_band(
    xs: list[float],
    ys: list[float],
    *,
    anchor: float,
    min_slope: float,
    max_slope: float,
) -> SlopeBandResult:
    """The largest contiguous run of sweep points containing the anchor over
    which the local slope stays inside ``[min_slope, max_slope]``.

    The anchor point is the sweep point whose ``x`` is nearest ``anchor``; the
    run grows outward from it while each neighbouring point's own slope is in
    the closed band. Edges are the outermost **in-band sample points** -- no
    interpolation -- so the reported span never exceeds the true one by
    construction (a finer sweep, not a convention, buys resolution).

    ``*_lo`` / ``*_hi`` are the smaller / larger of the two edge values, and
    ``*_span = *_hi - *_lo``. ``edge_lo_at`` / ``edge_hi_at`` describe the
    run's low-``x`` / high-``x`` end (the sweep axis, which is ordered
    unambiguously whatever ``y`` does): ``"band"`` when the slope left the
    band there, ``"sweep_end"`` when the run reached the end of the sweep and
    the true range may extend past it.

    Raises :class:`SlopeBandInputError` -- never returns a substituted value.
    An anchor whose own slope is outside the band is an error rather than a
    run of span ``0``: a zero span would grade against ``limits`` as a real
    (and usually failing) number, hiding the fact that the curve never
    satisfied the rule at the point the row is defined at.
    """
    _validate_sweep(xs, ys)
    xs, ys = _ascending(list(xs), list(ys))
    if not xs[0] <= anchor <= xs[-1]:
        raise SlopeBandInputError(
            REASON_ANCHOR_OUTSIDE,
            f"its anchor {anchor!r} lies outside the swept x range "
            f"[{xs[0]!r}, {xs[-1]!r}]",
        )
    slopes = local_slopes(xs, ys)
    anchor_i = min(range(len(xs)), key=lambda i: abs(xs[i] - anchor))

    def in_band(i: int) -> bool:
        return min_slope <= slopes[i] <= max_slope

    if not in_band(anchor_i):
        raise SlopeBandInputError(
            REASON_ANCHOR_OUT_OF_BAND,
            f"the local slope at its anchor point (x={xs[anchor_i]!r}, "
            f"nearest x to anchor {anchor!r}) is {slopes[anchor_i]!r}, outside "
            f"the band [{min_slope!r}, {max_slope!r}] -- the curve does not "
            "satisfy the rule at the point the row is defined at",
        )
    lo_i = anchor_i
    while lo_i > 0 and in_band(lo_i - 1):
        lo_i -= 1
    hi_i = anchor_i
    while hi_i < len(xs) - 1 and in_band(hi_i + 1):
        hi_i += 1
    y_edges = (ys[lo_i], ys[hi_i])
    return SlopeBandResult(
        y_lo=min(y_edges),
        y_hi=max(y_edges),
        y_span=max(y_edges) - min(y_edges),
        x_lo=xs[lo_i],
        x_hi=xs[hi_i],
        x_span=xs[hi_i] - xs[lo_i],
        edge_lo_at="sweep_end" if lo_i == 0 else "band",
        edge_hi_at="sweep_end" if hi_i == len(xs) - 1 else "band",
        points_in_run=hi_i - lo_i + 1,
    )
