"""Bind a gate-level Verilog instance's named port connections to the pins of
the cell/module it calls (split out of :mod:`verilog_netlist`; the size
ratchet, issue #4815)."""

from __future__ import annotations

import re


def _error_cls() -> type[Exception]:
    """`VerilogNetlistError`, imported late (that module imports this one)."""
    from .verilog_netlist import VerilogNetlistError

    return VerilogNetlistError


def _port_bit_pins(port: str, pins: list[str], *, library: bool) -> list[str]:
    """``port``'s per-bit pins among ``pins`` (every `<port>[<index>]`
    entry), MSB-first (issue #2657).

    A parsed sub-module's ports were bit-expanded by :func:`_expand_range`
    from its own `[msb:lsb]` declaration, so their declared order already is
    MSB-first, ascending range included. A library cell's pins come from a
    `.subckt`/`.cdl` header, whose bit names carry the Verilog index but
    whose header position carries nothing Verilog defines, so they are
    sorted by that index, highest first: the `[N-1:0]` convention every real
    vector-ported macro checked uses (see the module docstring)."""
    pattern = re.compile(rf"^{re.escape(port)}\[(-?\d+)\]$")
    hits = [
        (int(match.group(1)), pin)
        for pin in pins
        if (match := pattern.match(pin)) is not None
    ]
    if library:
        hits.sort(key=lambda hit: hit[0], reverse=True)
    return [pin for _, pin in hits]


def _case_fold_pin(
    instance: dict[str, object],
    port: str,
    pins: list[str],
    port_case_folds: list[dict[str, str]] | None,
) -> list[str]:
    """``[<pin>]`` when exactly one pin differs from scalar ``port`` only by
    letter case (recorded in ``port_case_folds`` when given), else ``[]``
    (no match, or ambiguous case-twins)."""
    folded = [pin for pin in pins if pin.casefold() == port.casefold()]
    if len(folded) != 1:
        return []
    if port_case_folds is not None:
        port_case_folds.append(
            {
                "cell": str(instance["cell"]),
                "instance": str(instance["name"]),
                "from": port,
                "to": folded[0],
            }
        )
    return folded


def _bind_connections(
    instance: dict[str, object],
    pins: list[str],
    *,
    library: bool,
    port_case_folds: list[dict[str, str]] | None = None,
) -> tuple[dict[str, str], set[str]]:
    """``({<real pin>: <net>}, {<unknown port>, ...})`` for one instance
    calling a cell/module with declared ``pins``.

    A scalar connection binds to its pin by name, as it always has. A
    multi-bit one (issue #2657) is zipped MSB-first onto the port's per-bit
    pins (:func:`_port_bit_pins`) -- or onto a scalar pin of that name, when
    one bit wide. A bit-count mismatch either way is an error naming the
    instance, port and both widths, never a silent truncate or pad. A port
    with no pin at all is returned as unknown for the caller to report (a
    library cell's error aggregates every such port across every instance
    of the cell type).

    Issue #4815: for a library cell only, a scalar port that is not an exact
    pin binds to the one pin differing from it only by letter case (synthesis
    emits the Liberty spelling, the CDL supplying pin order may spell it
    differently), recorded in ``port_case_folds`` when given. An exact match
    always wins; a pin list with two case-twins of the port is ambiguous and
    the port stays unknown.
    """
    pin_set = set(pins)
    pin_nets: dict[str, str] = {}
    unknown: set[str] = set()
    connections: dict[str, str | list[str]] = instance["connections"]  # type: ignore[assignment]
    for port, net in connections.items():
        if isinstance(net, str) and port in pin_set:
            bit_pins = [port]
        else:
            bit_pins = _port_bit_pins(port, pins, library=library)
            if not bit_pins and port in pin_set:
                bit_pins = [port]
            if not bit_pins and library and isinstance(net, str):
                bit_pins = _case_fold_pin(instance, port, pins, port_case_folds)
        if not bit_pins:
            unknown.add(port)
            continue
        bits = net if isinstance(net, list) else [net]
        if len(bits) != len(bit_pins):
            raise _error_cls()(
                f"instance '{instance['name']}' ('{instance['cell']}') port "
                f"'{port}' is {len(bit_pins)} bit(s) wide ({bit_pins[0]} .. "
                f"{bit_pins[-1]}) but its connection is {len(bits)} bit(s) wide"
            )
        for pin, bit in zip(bit_pins, bits, strict=True):
            if pin in pin_nets:
                raise _error_cls()(
                    f"instance '{instance['name']}' ('{instance['cell']}') "
                    f"connects pin '{pin}' twice"
                )
            pin_nets[pin] = bit
    return pin_nets, unknown
