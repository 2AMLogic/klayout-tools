"""Per-corner clock period maps for ``klt sta`` (issue #5240).

A ``constraints.clocks[].period_ns`` (or the single-clock shorthand's
period) may be a ``{corner: period}`` map instead of a scalar, valid only
with ``pdk.corners`` and with keys equal to those corner names exactly.
Split out of ``post_route_sta.py`` to respect the per-file size ratchet.
Errors are ``post_route_sta.PostRouteStaError`` (imported lazily: that
module imports this one).
"""

from __future__ import annotations

from typing import Any


def _error(message: str) -> Exception:
    from .post_route_sta import PostRouteStaError

    return PostRouteStaError(message)


def _period_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"{where} must be a positive number")
    if value <= 0:
        raise _error(f"{where} must be greater than zero")
    return float(value)


def validate_period(period: Any, where: str) -> float | dict[str, float]:
    """Validate one clock entry's ``period_ns``: a positive number, or a
    non-empty ``{corner: positive number}`` map (``where`` is the entry's
    path in the request)."""
    if not isinstance(period, dict):
        return _period_number(period, f"{where}.period_ns")
    if not period:
        raise _error(f"{where}.period_ns corner map must name at least one corner")
    return {
        corner: _period_number(value, f"{where}.period_ns['{corner}']")
        for corner, value in period.items()
    }


def has_period_maps(clock_period_ns: Any, clock_extras: dict[str, Any] | None) -> bool:
    return isinstance(clock_period_ns, dict) or any(
        isinstance(c["period_ns"], dict) for c in (clock_extras or {}).get("clocks", [])
    )


def check_period_maps(
    clock_period_ns: Any,
    clock_extras: dict[str, Any] | None,
    clock_port: str,
    requested_corners: list[str] | None,
) -> None:
    """A corner-keyed ``period_ns`` map is valid only with ``pdk.corners``,
    and its keys must equal those corner names exactly."""
    maps: list[tuple[str, dict[str, float]]] = []
    if isinstance(clock_period_ns, dict):
        maps.append((clock_port, clock_period_ns))
    for clock in (clock_extras or {}).get("clocks", []):
        if isinstance(clock["period_ns"], dict):
            maps.append((clock["port"], clock["period_ns"]))
    for port, mapping in maps:
        where = f"request.constraints.clocks (clock '{port}').period_ns"
        if requested_corners is None:
            raise _error(
                f"{where} is a per-corner map, which requires request.pdk.corners"
            )
        missing = [c for c in requested_corners if c not in mapping]
        extra = sorted(set(mapping) - set(requested_corners))
        if missing or extra:
            detail = []
            if missing:
                detail.append("missing corner(s): " + ", ".join(missing))
            if extra:
                detail.append("unknown corner(s): " + ", ".join(extra))
            raise _error(
                f"{where} keys must equal request.pdk.corners exactly "
                f"({'; '.join(detail)})"
            )


def clock_period_for_corner(
    corner: str | None,
    clock_port: str,
    clock_period_ns: Any,
    clock_extras: dict[str, Any] | None,
) -> tuple[float, dict[str, Any] | None, dict[str, Any]]:
    """Resolve per-corner period maps to scalars for one corner's session.

    Returns ``(period, extras, extra_response_fields)``; the last carries the
    corner's ``clock_targets`` echo only when a period map is in play."""
    mapped = has_period_maps(clock_period_ns, clock_extras)
    if isinstance(clock_period_ns, dict):
        clock_period_ns = clock_period_ns[corner]
    if mapped and clock_extras:
        clock_extras = {
            **clock_extras,
            "clocks": [
                {**c, "period_ns": c["period_ns"][corner]}
                if isinstance(c["period_ns"], dict)
                else c
                for c in clock_extras["clocks"]
            ],
        }
    fields = (
        {"clock_targets": clock_targets(clock_port, clock_period_ns, clock_extras)}
        if mapped
        else {}
    )
    return clock_period_ns, clock_extras, fields


def clock_targets(
    clock_port: str, clock_period_ns: float, clock_extras: dict[str, Any] | None
) -> dict[str, Any]:
    """The response's additive ``clock_targets`` echo (issue #4241): every
    clock applied (primary first), and the asynchronous groups."""
    clocks = [{"port": clock_port, "period_ns": clock_period_ns}]
    groups: list[list[str]] = []
    if clock_extras:
        clocks += clock_extras["clocks"]
        groups = clock_extras["groups"]
    return {"clocks": clocks, "clock_groups": groups}
