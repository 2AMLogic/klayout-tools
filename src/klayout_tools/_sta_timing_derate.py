"""``constraints.timing_derate`` (issue #7014): the request-side equivalent of
SDC ``set_timing_derate -early/-late``, split out of ``post_route_sta.py`` to
respect the per-file size ratchet (issue #2766).

The derate is one global, dimensionless factor per side, applied in every
analyzed corner of both input modes (``def`` and ``verilog``). Per-object,
per-corner and per-clock overrides are out of scope; so is an SDC
passthrough.
"""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_finite_number
from .post_route_sta_errors import PostRouteStaError

TIMING_DERATE_KEYS = ("early", "late")


def validate_timing_derate(
    constraints: dict[str, Any],
) -> dict[str, float | None] | None:
    """Validate ``constraints.timing_derate``, returning ``{"early": float |
    None, "late": float | None}`` (``None`` on a side means no derate on that
    side), or ``None`` when omitted or ``null`` (no ``set_timing_derate`` is
    emitted and the generated script is unchanged).

    The value must be an object with the optional keys ``early`` / ``late``
    (at least one required), each a finite JSON number ``> 0`` (``1.0`` is
    the neutral factor and is accepted). Booleans, strings, nested ``null``,
    non-finite numbers (including an integer too large for a float), zero,
    negative values, unknown keys, an empty object and any non-object value
    are request errors.
    """
    value = constraints.get("timing_derate")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PostRouteStaError(
            "constraints.timing_derate must be an object with 'early' and/or "
            f"'late' factors, got {type(value).__name__}"
        )
    unknown = sorted(set(value) - set(TIMING_DERATE_KEYS))
    if unknown:
        raise PostRouteStaError(
            "constraints.timing_derate has unknown key(s): "
            + ", ".join(unknown)
            + " (allowed: early, late)"
        )
    if not value:
        raise PostRouteStaError(
            "constraints.timing_derate must give at least one of 'early' and 'late'"
        )
    derate: dict[str, float | None] = {}
    for key in TIMING_DERATE_KEYS:
        if key not in value:
            derate[key] = None
            continue
        factor = value[key]
        name = f"constraints.timing_derate.{key}"
        if not is_finite_number(factor):
            raise PostRouteStaError(f"{name} must be a finite number, got {factor!r}")
        if factor <= 0:
            raise PostRouteStaError(f"{name} must be > 0, got {factor!r}")
        derate[key] = float(factor)
    return derate


def timing_derate_lines(timing_derate: dict[str, float | None] | None) -> list[str]:
    """One global ``set_timing_derate -early|-late <factor>`` per supplied
    side; empty when no derate was requested (an omitted side emits
    nothing)."""
    if not timing_derate:
        return []
    return [
        f"set_timing_derate -{key} {timing_derate[key]!r}"
        for key in TIMING_DERATE_KEYS
        if timing_derate.get(key) is not None
    ]


def timing_derate_block(
    timing_derate: dict[str, float | None] | None,
) -> dict[str, Any]:
    """The ``timing_derate`` response block: whether any derate was applied.
    A side not given is ``null`` (not derated), never copied from the
    other."""
    if not timing_derate:
        return {"applied": False}
    return {
        "applied": True,
        "early": timing_derate.get("early"),
        "late": timing_derate.get("late"),
    }
