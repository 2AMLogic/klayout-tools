"""``crossings`` on a ``dc`` sweep: the crossing *abscissa* (issue #5703).

A ``dc``-sweep measurement whose threshold is relative to a value measured in
the same corner -- "the input where ``v(out)`` has fallen to 99 % of its own
value at ``Vin = 1.0``" -- could not be declared, so the quantity could not
carry ``limits``. ``measurements[].crossings`` already resolves exactly that
threshold algebra per corner for ``tran``; this module lets it run on a
``dc`` analysis too, with three differences from the transient form:

- the series axis is the **swept source's value** (volts/amps), not seconds, so
  ``from``/``to``/``level.sample_at`` are in the sweep's own unit;
- the level may be the multiplicative ``{"sample_at": <sweep value>,
  "fraction": <k>}`` = ``k * signal(sample_at)`` (the ``{sample_at, offset}``
  form works on the sweep axis too);
- the graded ``value`` is the **first crossing abscissa**, not the count. The
  count and the whole abscissa list ride the additive ``crossings`` member.
  No crossing at all is a ``status: "error"`` measurement, never a ``0`` that
  passes a ``limits.min: 0``.

Everything else -- the rawfile written by the generated ``.control`` block,
:func:`~klayout_tools.sim_crossings.find_crossings` and its linear
interpolation -- is shared with the transient form.
"""

from __future__ import annotations

import math
from typing import Any

from . import sim_window
from ._jsonnum import is_number
from .sim_errors import SimError

#: ``analysis.kind`` values a dc crossing is extracted for.
DC_KIND = "dc"

#: The keys of the two sampled-level spellings (the dc-only one is
#: ``fraction``).
OFFSET_KEYS = frozenset({"sample_at", "offset"})
FRACTION_KEYS = frozenset({"sample_at", "fraction"})


def is_dc(kind: Any) -> bool:
    """Whether ``analysis.kind`` names a dc sweep."""
    return str(kind or "").lower() == DC_KIND


def is_fraction_level(level: Any) -> bool:
    """Whether ``level`` is the dc-only ``{sample_at, fraction}`` form."""
    return isinstance(level, dict) and "sample_at" in level and "fraction" in level


def validate_fraction(name: Any, level: dict[str, Any]) -> None:
    """Shape-check the ``fraction`` of a ``{sample_at, fraction}`` level."""
    fraction = level["fraction"]
    if not is_number(fraction) or not math.isfinite(fraction):
        raise SimError(
            f"measurement '{name}': crossings.level.fraction must be a finite "
            f"number (got {fraction!r})"
        )


def validate_dc_context(
    declared: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """Refuse, before any run, what a dc crossing cannot honour: a ``reduce``
    (period/TIE reductions are time-series quantities) and a literal
    ``sample_at`` outside the literal sweep range."""
    from .sim import _dc_sweep_bounds

    bounds = _dc_sweep_bounds(analysis)
    for spec in declared:
        name, body = spec["name"], spec["crossings"]
        if body.get("reduce") is not None:
            raise SimError(
                f"measurement '{name}': crossings.reduce is a time-series "
                "reduction and is not supported on a dc sweep -- drop it"
            )
        level = body.get("level")
        if bounds is None or not isinstance(level, dict) or "sample_at" not in level:
            continue
        at = sim_window.window_bound(
            name, level, "sample_at", form="crossings", field="level.sample_at"
        )
        low, high = min(bounds), max(bounds)
        if at is not None and not low <= at <= high:
            raise SimError(
                f"measurement '{name}': crossings.level.sample_at ({at!r}) is "
                f"outside the dc sweep range [{low!r}, {high!r}] -- pick a "
                "sweep value inside it"
            )


def validate_tran_only_levels(declared: list[dict[str, Any]], kind: Any) -> None:
    """``{sample_at, fraction}`` is defined for the dc axis only."""
    for spec in declared:
        if is_fraction_level(spec["crossings"].get("level")):
            raise SimError(
                f"measurement '{spec['name']}': crossings.level "
                '{"sample_at", "fraction"} is only defined for analysis.kind '
                f'"dc" (got {kind!r}); on tran use {{"sample_at", "offset"}}'
            )


def resolve_sampled_level(
    name: Any,
    level: dict[str, Any],
    scale: list[float],
    values: list[float],
    *,
    sweep: bool = True,
) -> float:
    """``signal(sample_at) * fraction`` or ``signal(sample_at) + offset``, the
    sample linearly interpolated; the axis is a dc sweep (either direction) or,
    with ``sweep=False``, simulated time. Raises ``ValueError`` (the
    measurement diagnostic) when ``sample_at`` is outside the axis range."""
    at = sim_window.window_bound(
        name, level, "sample_at", form="crossings", field="level.sample_at"
    )
    assert at is not None  # validated as required before any run
    count = min(len(scale), len(values))
    low = min(scale[:count], default=math.nan)
    high = max(scale[:count], default=math.nan)
    if count == 0 or not low <= at <= high:
        span = f"[{low!r}, {high!r}]" if count else "empty"
        unit, axis = ("", "swept") if sweep else (" s", "simulated time")
        raise ValueError(
            f"measurement '{name}': crossings.level.sample_at ({at!r}{unit}) "
            f"is outside the {axis} range {span}{unit if count else ''}"
        )
    sampled = values[count - 1]
    for index in range(1, count):
        lo, hi = sorted((scale[index - 1], scale[index]))
        if lo <= at <= hi:
            x0, x1 = scale[index - 1], scale[index]
            y0, y1 = values[index - 1], values[index]
            sampled = y1 if x1 == x0 else y0 + (y1 - y0) * (at - x0) / (x1 - x0)
            break
    if "fraction" in level:
        return sampled * float(level["fraction"])
    return sampled + float(level["offset"])


def no_crossing_message(name: Any, level: float, edge: str) -> str:
    """The error for a dc crossing that never happens in the window."""
    return (
        f"measurement '{name}': the signal never crosses {level!r} "
        f"({edge} edge) inside the swept range, so there is no crossing "
        "abscissa to grade -- widen the sweep or the from/to window, or "
        "check the threshold"
    )
