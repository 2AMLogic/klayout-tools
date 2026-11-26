"""Root-clock constraints shared by the P&R stages and post-route timing sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._jsonnum import is_finite_number
from ._tcl import tcl_literal as _tcl_literal
from .place_and_route_base import PlaceAndRouteError

VIRTUAL_CLOCK_NAME = "klt_virtual_clk"


def _pattern(port: str) -> str:
    # OpenSTA consumes a Tcl list of patterns even for a single port.
    return (
        f"[list {_tcl_literal(port)}]"
        if any(c.isspace() or c in '{}\\"' for c in port)
        else _tcl_literal(port)
    )


@dataclass(frozen=True)
class ClockConstraints:
    port: str | None = None
    period_ns: float | None = None
    secondary: tuple[tuple[str, float], ...] = ()
    groups: tuple[tuple[str, ...], ...] = ()

    @property
    def ports(self) -> tuple[str, ...]:
        if self.port is None:
            return ()
        return (self.port, *(port for port, _ in self.secondary))

    def declaration_lines(self) -> list[str]:
        if self.port is None:
            return virtual_clock_lines(self.period_ns, _UNIT_PIN_LINE)
        lines = [_UNIT_PIN_LINE]
        for port, period in ((self.port, self.period_ns), *self.secondary):
            name = _tcl_literal(port)
            lines += [
                f"set klt_root [get_ports -quiet {_pattern(port)}]",
                "if {[llength $klt_root] != 1 || "
                f"![string equal [get_full_name $klt_root] {name}]}} {{",
                f"    error [format {{cannot resolve clock port '%s'}} {name}]",
                "}",
                f"create_clock -name {name} -period {period} $klt_root",
            ]
        if self.groups:
            lines.append(
                "set_clock_groups -asynchronous "
                + " ".join(
                    "-group [get_clocks [list "
                    + " ".join(map(_tcl_literal, group))
                    + "]]"
                    for group in self.groups
                )
            )
        return lines

    def delay_lines(
        self, input_delay_ns: float | None, output_delay_ns: float | None
    ) -> list[str]:
        return _io_delay_lines(
            self.port, input_delay_ns, output_delay_ns, self.secondary
        )

    def inventory_lines(self) -> list[str]:
        if self.port is None:
            return []
        # Use exact engine names as dictionary keys. A missing clock aborts the
        # stage. Hex keeps arbitrary HDL identifiers out of JSON metric keys.
        ports = " ".join(map(_tcl_literal, self.ports))
        return [
            "set klt_roots [dict create]",
            "foreach clk [all_clocks] {dict set klt_roots [get_name $clk] $clk}",
            f"foreach port [list {ports}] {{",
            "    set clk [dict get $klt_roots $port]",
            "    set key [binary encode hex [encoding convertto utf-8 $port]]",
            "    utl::metric_float clock__period_ns__$key [get_property $clk period]",
            "    utl::metric_integer clock__registers__$key "
            "[llength [all_registers -clock $clk]]",
            "}",
        ]

    def inventory(self, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        roots = []
        for port in self.ports:
            key = port.encode("utf-8").hex()
            try:
                period = metrics[f"clock__period_ns__{key}"]
                registers = metrics[f"clock__registers__{key}"]
            except KeyError as exc:
                raise PlaceAndRouteError(
                    f"OpenROAD did not report the inventory for clock port {port!r}"
                ) from exc
            roots.append(
                {"port": port, "period_ns": period, "clocked_registers": registers}
            )
        return roots


def _period(value: Any, field: str) -> float:
    if not is_finite_number(value) or value <= 0:
        raise PlaceAndRouteError(
            f"{field} must be a positive number (finite nanoseconds)"
        )
    return float(value)


def _root(entry: Any, where: str) -> tuple[str, float]:
    if not isinstance(entry, dict):
        raise PlaceAndRouteError(f"{where} must be a {{port, period_ns}} object")
    if unknown := entry.keys() - {"port", "period_ns"}:
        raise PlaceAndRouteError(
            f"{where} has unknown field(s): {', '.join(sorted(unknown))}"
        )
    port = entry.get("port")
    if not isinstance(port, str) or not port.strip():
        raise PlaceAndRouteError(f"{where}.port must be a non-empty string")
    return port, _period(entry.get("period_ns"), f"{where}.period_ns")


def _groups(raw: Any, ports: set[str]) -> tuple[tuple[str, ...], ...]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise PlaceAndRouteError(
            "request.constraints.clock_groups needs at least two groups"
        )
    groups = []
    for i, group in enumerate(raw):
        field = f"request.constraints.clock_groups[{i}]"
        if not isinstance(group, list) or not group:
            raise PlaceAndRouteError(f"{field} must be a non-empty array")
        for port in group:
            if not isinstance(port, str) or port not in ports:
                raise PlaceAndRouteError(f"{field} names an undeclared clock: {port!r}")
        groups.append(tuple(dict.fromkeys(group)))
    return tuple(groups)


def _validate_root_list(constraints: dict[str, Any]) -> ClockConstraints:
    if mixed := constraints.keys() & {"clock_port", "clock_period_ns"}:
        raise PlaceAndRouteError(
            "request.constraints.clocks cannot be combined with "
            + ", ".join(sorted(mixed))
        )
    raw = constraints["clocks"]
    if not isinstance(raw, list) or not raw:
        raise PlaceAndRouteError("request.constraints.clocks must be a non-empty array")
    roots = {}
    for i, entry in enumerate(raw):
        field = f"request.constraints.clocks[{i}]"
        port, period = _root(entry, field)
        if port in roots:
            raise PlaceAndRouteError(f"{field}.port {port!r} is listed more than once")
        roots[port] = period
    (port, period), *secondary = roots.items()
    groups = (
        _groups(constraints["clock_groups"], set(roots))
        if "clock_groups" in constraints
        else ()
    )
    return ClockConstraints(port, period, tuple(secondary), groups)


def validate_clock_constraints(constraints: dict[str, Any]) -> ClockConstraints:
    if "clocks" in constraints:
        return _validate_root_list(constraints)
    if "clock_groups" in constraints:
        raise PlaceAndRouteError(
            "request.constraints.clock_groups requires constraints.clocks"
        )
    port = constraints.get("clock_port")
    period = constraints.get("clock_period_ns")
    if port is None and period is None:
        return ClockConstraints()
    if port is not None and not (isinstance(port, str) and port.strip()):
        raise PlaceAndRouteError(
            "request.constraints.clock_port must be a non-empty string"
        )
    return ClockConstraints(
        port, _period(period, "request.constraints.clock_period_ns")
    )


# Pins command units to ns / pF so the unit-less constraint numbers below and
# the metric procs read back are Liberty-``time_unit``-independent (#4089).
_UNIT_PIN_LINE = "set_cmd_units -time ns -capacitance pF"


def _clock_lines(clock_port: str | None, clock_period_ns: float | None) -> list[str]:
    """Create a clock after link_design/read_db resolves [get_ports] (#425)."""
    return ClockConstraints(clock_port, clock_period_ns).declaration_lines()


def _io_delay_lines(
    clock_port: str | None,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    secondary: tuple[tuple[str, float], ...] = (),
) -> list[str]:
    if clock_port is None:
        return virtual_io_delay_lines(input_delay_ns, output_delay_ns)
    lines: list[str] = []
    if input_delay_ns is not None:
        lines += [
            f"set klt_clock_port [get_ports {_pattern(clock_port)}]",
            "set klt_non_clock_inputs "
            "[lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]",
            *(
                "set klt_non_clock_inputs [lsearch -inline -all -not -exact "
                f"$klt_non_clock_inputs [get_ports {_pattern(port)}]]"
                for port, _ in secondary
            ),
            f"set_input_delay {input_delay_ns} "
            f"-clock {_tcl_literal(clock_port)} $klt_non_clock_inputs",
        ]
    if output_delay_ns is not None:
        lines.append(
            f"set_output_delay {output_delay_ns} "
            f"-clock {_tcl_literal(clock_port)} [all_outputs]"
        )
    return lines


def virtual_clock_lines(clock_period_ns: float | None, unit_line: str) -> list[str]:
    """``create_clock`` for a port-less clock (``[]`` without a period)."""
    if clock_period_ns is None:
        return []
    return [
        unit_line,
        f"create_clock -name {VIRTUAL_CLOCK_NAME} -period {clock_period_ns}",
    ]


def virtual_io_delay_lines(
    input_delay_ns: float | None,
    output_delay_ns: float | None,
) -> list[str]:
    """``set_input_delay``/``set_output_delay`` against the virtual clock.

    No port to exclude, so the input delay applies to every input. Request
    validation guarantees a period whenever a delay is set.
    """
    lines = []
    if input_delay_ns is not None:
        lines.append(
            f"set_input_delay {input_delay_ns} -clock {VIRTUAL_CLOCK_NAME} [all_inputs]"
        )
    if output_delay_ns is not None:
        lines.append(
            f"set_output_delay {output_delay_ns} "
            f"-clock {VIRTUAL_CLOCK_NAME} [all_outputs]"
        )
    return lines
