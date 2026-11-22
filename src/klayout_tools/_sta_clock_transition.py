"""Ideal-clock transition and propagated-clock selection for STA requests."""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_finite_number
from ._tcl import tcl_literal


def _error(message: str) -> Exception:
    from .post_route_sta import PostRouteStaError

    return PostRouteStaError(message)


def validate_clock_transition(constraints: dict[str, Any]) -> float | None:
    value = constraints.get("clock_transition_ns")
    if value is None:
        return None
    if not is_finite_number(value) or value < 0:
        raise _error("constraints.clock_transition_ns must be a finite number >= 0")
    if constraints.get("propagated_clock") is True:
        raise _error(
            "constraints.clock_transition_ns conflicts with propagated_clock: "
            "OpenSTA ignores set_clock_transition on a propagated clock"
        )
    return float(value)


def transition_lines(clock_port: str, ns: float | None) -> list[str]:
    if ns is None:
        return []
    return [f"set_clock_transition {ns} [get_clocks {tcl_literal(clock_port)}]"]


def transition_block(ns: float | None) -> dict[str, Any]:
    return {"applied": False} if ns is None else {"applied": True, "ns": ns}


def validate_propagated_clock(constraints: dict[str, Any], has_def: bool) -> bool:
    """Validate ``constraints.propagated_clock`` (issue #2739), returning the
    validated boolean (``False`` when omitted or ``null`` -- the ideal-clock
    default this command has always run, byte-for-byte).

    Only a JSON boolean is accepted; every other non-null value is a request
    error. ``True`` is **def-mode only**: a ``verilog``-mode request links a
    structural netlist with no placement at all, so there is no placed clock
    tree to propagate through and the option is rejected with an actionable
    error rather than silently ignored.

    Meaningful is not the same as *correct* for a ``def``-mode request: the
    option is only worth what the DEF's clock tree is worth (a pre-CTS DEF
    propagates through plain wires, arriving with ~no insertion delay), and
    this module cannot infer tree presence from ``geometry_source`` or from
    a non-null ``clock_skew_ns`` -- the response's ``clock_propagation``
    evidence block is what says what propagation actually reached.

    ``constraints`` is already known to be a JSON object by the time this is
    called -- :func:`_validate_constraints` (always called first) raises
    otherwise.
    """
    value = constraints.get("propagated_clock")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _error(
            "constraints.propagated_clock must be a boolean (true enables "
            "propagated-clock timing in def mode; omit or false keeps the "
            "ideal-clock default)"
        )
    if value and not has_def:
        raise _error(
            "constraints.propagated_clock is only valid in def mode -- a "
            "verilog-mode request links a netlist with no placed clock tree "
            "to propagate through"
        )
    return value
