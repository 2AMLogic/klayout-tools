"""Named synthesis clocks and exception-aware STA on the final mapped Verilog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import macros as macro_spec
from ._jsonnum import is_number
from ._sta_multicycle import validate_multicycle_paths
from .post_route_sta_errors import PostRouteStaError
from .synthesize_abc_stages import SynthesizeError


def _resolve_clocks(constraints: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The validated ``constraints.clocks`` list as ``[{port, period_ns}]``
    (issue #2883), empty when omitted/``null``.

    Each entry must be an object with a non-empty string ``port`` (unique
    across the list) and a positive numeric ``period_ns``. Unknown entry keys
    and a non-array / empty list are errors -- a request that means to
    constrain several clocks but expresses it wrongly must not be silently
    downgraded, exactly as for ``clock_period_ns``.
    """
    if not constraints:
        return []
    raw = constraints.get("clocks")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise SynthesizeError(
            "request.constraints.clocks must be a non-empty array of "
            "{port, period_ns} objects or null"
        )
    clocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"request.constraints.clocks[{index}]"
        clock = _validate_clock_entry(entry, where)
        port = clock["port"]
        if port in seen:
            raise SynthesizeError(f"{where}.port '{port}' is listed more than once")
        seen.add(port)
        clocks.append(clock)
    return clocks


def _validate_clock_entry(entry: Any, where: str) -> dict[str, Any]:
    """One validated ``constraints.clocks`` entry as ``{port, period_ns}``
    (issue #2883).

    Checks the entry in isolation -- object shape, no unknown keys, a
    non-empty string ``port`` and a positive numeric ``period_ns``. The
    list-level checks (array type, non-empty, unique ports) stay in
    :func:`_resolve_clocks`. ``where`` prefixes every error message.
    """
    if not isinstance(entry, dict):
        raise SynthesizeError(f"{where} must be a {{port, period_ns}} object")
    unknown = sorted(set(entry) - {"port", "period_ns"})
    if unknown:
        raise SynthesizeError(
            f"{where} has unknown key(s): {', '.join(map(str, unknown))} "
            "(allowed: port, period_ns)"
        )
    port = entry.get("port")
    if not isinstance(port, str) or not port.strip():
        raise SynthesizeError(f"{where}.port must be a non-empty string")
    if not is_number(period := entry.get("period_ns")):
        raise SynthesizeError(
            f"{where}.period_ns must be a positive number (nanoseconds)"
        )
    if period <= 0:
        raise SynthesizeError(f"{where}.period_ns must be greater than zero")
    return {"port": port, "period_ns": period}


def mapped_sta_constraints(constraints: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate before synthesis. Only named clocks convey a port to OpenSTA."""
    if constraints is None:
        return None
    try:
        entries = validate_multicycle_paths(constraints)
    except PostRouteStaError as exc:
        raise SynthesizeError(str(exc)) from exc
    if entries is None:
        return None
    clocks = _resolve_clocks(constraints)
    if not clocks:
        raise SynthesizeError(
            "constraints.multicycle_paths[0] needs a clock port: "
            "supply constraints.clocks with port and period_ns"
        )
    # run_sta accepts the request spelling, not the validator's indexed records.
    return {"clocks": clocks, "multicycle_paths": constraints["multicycle_paths"]}


def constrained_timing_fields(
    constraints: dict[str, Any] | None,
    *,
    netlist_path: str,
    hdl_toplevel: str,
    cell_library: str,
    corner: str,
    macros: list[dict[str, Any]],
    pdk_variant: str | None,
    pdk_root: str | None,
) -> dict[str, Any]:
    if constraints is None:
        return {}
    from .post_route_sta import run_sta

    netlist = Path(netlist_path)
    request_path = netlist.with_name("constrained_sta_request.json")
    request = {
        "verilog": netlist.name,
        "hdl_toplevel": hdl_toplevel,
        "pdk": {"cell_library": cell_library, "corner": corner},
        "constraints": constraints,
        # These paths were resolved relative to the synthesis request. Keep
        # those inputs when running STA from the synthesis artifact directory.
        "macros": macro_spec.macros_response(macros),
    }
    try:
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        report = run_sta(str(request_path), pdk_variant=pdk_variant, pdk_root=pdk_root)
    except (OSError, PostRouteStaError) as exc:
        count = len(constraints["multicycle_paths"])
        scope = "constraints.multicycle_paths[0]"
        if count > 1:
            scope += f" through constraints.multicycle_paths[{count - 1}]"
        raise SynthesizeError(f"mapped-netlist STA for {scope}: {exc}") from exc
    return {
        "constrained_timing": {
            "basis": "klt sta Verilog mode on the mapped netlist",
            "geometry_source": report["geometry_source"],
            "worst_slack_ns": report["worst_slack_ns"],
            "worst_hold_slack_ns": report["worst_hold_slack_ns"],
            "timing_status": report["timing_status"],
            "multicycle_paths": report["multicycle_paths"],
        }
    }
