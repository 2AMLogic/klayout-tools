"""Per-entry clock references for ``klt sta`` I/O delays (issue #7399).

``constraints.input_delays``/``.output_delays`` entries gain an optional
``clock`` key naming which declared clock the delay is relative to
(``{"<port>": {"clock": "dqs", "min_ns": ..}}``), so a source-synchronous
capture path can be constrained against a secondary strobe clock (issue
#4241's ``constraints.clocks``) instead of only the primary. Omitted, the
primary clock is used and the generated Tcl is byte-identical. Split out of
``post_route_sta.py`` (per-file size ratchet); validation raises the shared
``PostRouteStaError``.
"""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_finite_number
from .post_route_sta_errors import PostRouteStaError

#: One validated ``input_delays``/``.output_delays`` entry: the two optional
#: bounds plus (issue #7399) the optional literal declared-clock name.
IoDelayEntry = dict[str, float | str]
#: One validated per-port I/O delay map (literal port name -> entry).
IoDelayMap = dict[str, IoDelayEntry]


def entry_clock(entry: IoDelayEntry | None, clock_port: str) -> str:
    """The ``-clock`` reference for one entry's emission: the entry's own
    ``clock`` when given, else the primary clock."""
    value = (entry or {}).get("clock")
    return value if isinstance(value, str) else clock_port


def entry_bound(entry: IoDelayEntry, side: str) -> float | None:
    """The entry's ``min_ns``/``max_ns`` bound (``None`` when omitted)."""
    value = entry.get(f"{side}_ns")
    return value if isinstance(value, float) else None


def validate_port_delay_maps(
    constraints: dict[str, Any],
    clock_port: str,
    declared_clocks: set[str] | None = None,
) -> tuple[IoDelayMap | None, IoDelayMap | None]:
    """``constraints.input_delays``/``.output_delays`` (issue #2740):
    optional per-port, per-bound I/O delay maps. Returns ``(input_delays,
    output_delays)``, each ``None`` when the field is omitted *or* empty
    (an empty map constrains nothing, so it reproduces the scalar-only
    generated Tcl byte-for-byte).

    Each map is ``{"<literal port name>": {"min_ns": n, "max_ns": n}}``
    with both bounds independently optional but at least one required. A
    supplied bound is never copied to the other side: an entry with only
    ``min_ns`` constrains only the min (hold-side) arrival of that port, and
    its max side stays unconstrained -- the scalar default does **not** fill
    it in, because an explicit entry replaces the scalar default for that
    port entirely.

    Unlike the legacy scalars (non-negative), per-port bounds may be
    negative (a boundary reference point can legitimately sit after the
    launching edge); they must still be finite, non-boolean numbers, and
    ``min_ns <= max_ns`` when both are given.

    Keys are literal port (or single bus-bit) names -- never Tcl, never a
    wildcard pattern. Existence and direction can only be checked against
    the loaded design, so they are verified inside the engine session
    (:func:`post_route_sta._port_check_lines`); the clock port is rejected
    here, since ``constraints.clock_port`` already names it.

    Issue #7399: each entry may additionally carry ``clock`` -- a non-empty
    string naming a clock declared in the same request (the primary or any
    ``constraints.clocks`` entry; ``declared_clocks``, defaulting to just
    the primary). The entry's ``set_input_delay``/``set_output_delay`` lines
    are then emitted ``-clock`` that clock instead of the primary. ``clock``
    alone (no bound) is an error: it constrains nothing.
    """
    declared = {clock_port} if declared_clocks is None else declared_clocks
    return (
        validate_port_delay_map(constraints, "input", clock_port, declared),
        validate_port_delay_map(constraints, "output", clock_port, declared),
    )


def validate_port_delay_map(
    constraints: dict[str, Any],
    which: str,
    clock_port: str,
    declared_clocks: set[str],
) -> IoDelayMap | None:
    """Validate one of ``constraints.{input,output}_delays`` (see
    :func:`validate_port_delay_maps`)."""
    field = f"request.constraints.{which}_delays"
    value = constraints.get(f"{which}_delays")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PostRouteStaError(
            f"{field} must be a JSON object mapping port names to "
            '{"min_ns": ..., "max_ns": ...} entries'
        )
    parsed: IoDelayMap = {}
    for port, entry in value.items():
        check_port_delay_key(field, which, port, clock_port)
        parsed[port] = parse_port_delay_entry(field, port, entry, declared_clocks)
    return parsed or None


def check_port_delay_key(field: str, which: str, port: str, clock_port: str) -> None:
    """Reject a malformed port-name key of an I/O delay map."""
    if not port or port != port.strip():
        raise PostRouteStaError(
            f"{field} keys must be non-empty port names without "
            f"leading/trailing whitespace (got {port!r})"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in port):
        raise PostRouteStaError(f"{field} key {port!r} contains a control character")
    if which == "input" and port == clock_port:
        raise PostRouteStaError(
            f"{field} must not name the clock port {port!r} -- "
            "clock inputs are excluded from input delays"
        )


def parse_port_delay_entry(
    field: str, port: str, entry: Any, declared_clocks: set[str]
) -> IoDelayEntry:
    """Validate one ``{"min_ns": n, "max_ns": n, "clock": c}`` entry of an
    I/O delay map (issue #7399 added the optional ``clock``)."""
    if not isinstance(entry, dict):
        raise PostRouteStaError(
            f"{field}[{port!r}] must be a JSON object with 'min_ns' and/or "
            "'max_ns' (optionally 'clock')"
        )
    unknown = sorted(set(entry) - {"min_ns", "max_ns", "clock"})
    if unknown:
        raise PostRouteStaError(
            f"{field}[{port!r}] has unknown key(s) "
            + ", ".join(repr(k) for k in unknown)
            + " -- only 'min_ns', 'max_ns' and 'clock' are accepted"
        )
    if not entry:
        raise PostRouteStaError(
            f"{field}[{port!r}] is empty -- give 'min_ns', 'max_ns', or both"
        )
    values: dict[str, float] = {}
    for bound in ("min_ns", "max_ns"):
        if bound not in entry:
            continue
        bound_value = entry[bound]
        if not is_finite_number(bound_value):
            raise PostRouteStaError(
                f"{field}[{port!r}].{bound} must be a finite number "
                f"(got {bound_value!r})"
            )
        values[bound] = float(bound_value)
    clock: str | None = None
    if "clock" in entry:
        given = entry["clock"]
        if not isinstance(given, str) or not given:
            raise PostRouteStaError(
                f"{field}[{port!r}].clock must be a non-empty string naming "
                "a declared clock"
            )
        if given not in declared_clocks:
            raise PostRouteStaError(
                f"{field}[{port!r}].clock names '{given}', which is not a "
                "declared clock (declared: " + ", ".join(sorted(declared_clocks)) + ")"
            )
        clock = given
    if not values:
        raise PostRouteStaError(
            f"{field}[{port!r}] has no bound -- 'clock' alone constrains "
            "nothing; give 'min_ns', 'max_ns', or both"
        )
    if (
        "min_ns" in values
        and "max_ns" in values
        and values["min_ns"] > values["max_ns"]
    ):
        raise PostRouteStaError(
            f"{field}[{port!r}].min_ns ({values['min_ns']}) must not "
            f"exceed max_ns ({values['max_ns']})"
        )
    bounds: IoDelayEntry = dict(values)
    if clock is not None:
        bounds["clock"] = clock
    return bounds
