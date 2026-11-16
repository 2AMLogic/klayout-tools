"""``constraints.clock_uncertainty_ns`` validation (issue #4240), split out of
``post_route_sta.py`` to respect the per-file size ratchet (issue #2766)."""

from __future__ import annotations

import math
from typing import Any

CLOCK_UNCERTAINTY_KEYS = ("setup_ns", "hold_ns")


def _error(message: str) -> Exception:
    from .post_route_sta import PostRouteStaError

    return PostRouteStaError(message)


def validate_clock_uncertainty(
    constraints: dict[str, Any],
) -> dict[str, float | None] | None:
    """Validate ``constraints.clock_uncertainty_ns`` (issue #4240), returning
    ``{"setup_ns": float | None, "hold_ns": float | None}`` (``None`` on a
    side means unconstrained), or ``None`` when omitted or ``null`` (no
    ``set_clock_uncertainty`` is emitted and the generated script is
    unchanged).

    The value is either a finite number ``>= 0`` (applied to setup and hold)
    or an object with the optional keys ``setup_ns`` / ``hold_ns`` (each a
    finite number ``>= 0``, at least one required). Booleans, non-finite or
    negative numbers, unknown keys and any other type are request errors.
    """
    value = constraints.get("clock_uncertainty_ns")
    if value is None:
        return None

    def _number(number: Any, name: str) -> float:
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
        ):
            raise _error(f"{name} must be a finite number")
        if number < 0:
            raise _error(f"{name} must be >= 0, got {number}")
        return float(number)

    if not isinstance(value, dict):
        scalar = _number(value, "constraints.clock_uncertainty_ns")
        return {"setup_ns": scalar, "hold_ns": scalar}
    unknown = sorted(set(value) - set(CLOCK_UNCERTAINTY_KEYS))
    if unknown:
        raise _error(
            "constraints.clock_uncertainty_ns has unknown key(s): "
            + ", ".join(unknown)
            + " (allowed: setup_ns, hold_ns)"
        )
    if not value:
        raise _error(
            "constraints.clock_uncertainty_ns must give at least one of "
            "'setup_ns' and 'hold_ns'"
        )
    return {
        key: (
            _number(value[key], f"constraints.clock_uncertainty_ns.{key}")
            if value.get(key) is not None
            else None
        )
        for key in CLOCK_UNCERTAINTY_KEYS
    }
