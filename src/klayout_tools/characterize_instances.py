"""Per-instance testbench netlist helpers for ``klt characterize``: the
pin-capacitance and leakage instances and the rail-net maps they share.

Split out of :mod:`klayout_tools.characterize` (size ratchet, issue #4756).
"""

from __future__ import annotations

import re
from typing import Any

from . import liberty_writer
from .characterize_arcs import DerivedArc


def _spice_number(value: float) -> str:
    return liberty_writer.format_number(value, precision=10)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _metered_rail_nodes(
    tag: str, cell: dict[str, Any], rail_nodes: dict[str, str]
) -> dict[str, str]:
    """``rail_nodes`` with the cell's primary ``vdd``/``gnd`` terminals moved
    onto this instance's private ammeter nodes."""
    nodes = dict(rail_nodes)
    nodes[cell["power_pins"]["vdd"]] = f"pd_{tag}"
    nodes[cell["power_pins"]["gnd"]] = f"pg_{tag}"
    return nodes


def _capacitance_side_state(
    pin_name: str, arcs: tuple[DerivedArc, ...], cell: dict[str, Any]
) -> tuple[tuple[str, bool], ...]:
    """The state the cell's *other* inputs are held at while ``pin_name``'s
    capacitance is measured.

    The acceptance bar (issue #2512) is "the same side-input states the
    timing arcs use", so this reuses the first derived arc whose
    ``related_pin`` is this pin -- the same state, in the same deterministic
    order, that arc's delay tables were measured at. That matters: an input
    of a NAND held at the *controlling* state would see no output movement
    and therefore no Miller charge, reporting a capacitance well below the
    one the pin actually presents in the situation a timing analysis cares
    about.

    An input pin no arc names (an unused pin, or a second output's input on a
    multi-output cell) has no such state; its neighbours are held low, the
    same fallback :func:`_full_side_state` applies to an unreferenced pin.
    """
    for arc in arcs:
        if arc.related_pin == pin_name:
            return _full_side_state(arc, cell)
    return tuple((pin, False) for pin in cell["input_pins"] if pin != pin_name)


def _capacitance_instance_lines(
    *,
    tag: str,
    cell: dict[str, Any],
    pin_name: str,
    side_state: tuple[tuple[str, bool], ...],
    rail_nodes: dict[str, str],
    vdd: float,
    ramp_ns: float,
    load_pf: float,
    rise_start: float,
    fall_start: float,
) -> list[str]:
    """One never-metered-supply instance whose single job is to let the charge
    drawn by ``pin_name`` be integrated.

    The pin is fed from a ramp source through a 0 V ammeter (``Vcm<tag>``),
    so ``i(Vcm<tag>)`` is the current flowing *into* the pin; its integral
    over an input edge is the charge that edge moved. Supplies go straight to
    the shared rails -- this instance contributes to no power or leakage
    measurement, so it needs no ammeter pair of its own.
    """
    side_map = dict(side_state)
    node_of: dict[str, str] = {}
    for pin in cell["pins"]:
        name = pin["name"]
        if pin["direction"] == "output":
            node_of[name] = f"o_{tag}_{_slug(name)}"
        elif name == pin_name:
            node_of[name] = f"ci_{tag}"
        else:
            node_of[name] = "vdd" if side_map[name] else "0"

    nodes = [
        node_of.get(terminal) or rail_nodes[terminal] for terminal in cell["terminals"]
    ]
    lines = [
        f"Vc{tag} cs_{tag} 0 PWL(0 0 "
        f"{_spice_number(rise_start)}n 0 "
        f"{_spice_number(rise_start + ramp_ns)}n {_spice_number(vdd)} "
        f"{_spice_number(fall_start)}n {_spice_number(vdd)} "
        f"{_spice_number(fall_start + ramp_ns)}n 0)",
        # 0 V ammeter in series with the pin: i(Vcm<tag>) is the current the
        # source delivers INTO the pin.
        f"Vcm{tag} cs_{tag} ci_{tag} DC 0",
        f"X{tag} {' '.join(nodes)} {cell['subckt']}",
    ]
    for output in cell["output_pins"]:
        lines.append(
            f"C{tag}_{_slug(output)} {node_of[output]} 0 {_spice_number(load_pf)}p"
        )
    return lines


def _leakage_states(cell: dict[str, Any]) -> tuple[tuple[tuple[str, bool], ...], ...]:
    """Every assignment of the cell's input pins, in a fixed order (the first
    input is the most significant bit), each as ``((pin, value), ...)``.

    Exhaustive over ``2**n`` for the same reason arc derivation is: a
    standard cell has a handful of inputs, and a vendor ``.lib`` reports one
    ``leakage_power`` group per state.
    """
    inputs = cell["input_pins"]
    states = []
    for code in range(2 ** len(inputs)):
        states.append(
            tuple(
                (pin, bool((code >> (len(inputs) - 1 - position)) & 1))
                for position, pin in enumerate(inputs)
            )
        )
    return tuple(states)


def _leakage_instance_lines(
    *,
    tag: str,
    cell: dict[str, Any],
    state: tuple[tuple[str, bool], ...],
    rail_nodes: dict[str, str],
) -> list[str]:
    values = dict(state)
    node_of: dict[str, str] = {}
    for pin in cell["pins"]:
        name = pin["name"]
        if pin["direction"] == "output":
            node_of[name] = f"o_{tag}_{_slug(name)}"
        else:
            # A high input is tied to the *metered* supply node, not the
            # shared rail: its gate leakage is drawn from the supply too, and
            # static power is every source's V * I -- a low input's source
            # sits at 0 V and delivers none. Measured on IHP sg13g2_inv_1
            # with A high, routing the input around the ammeter under-reports
            # the state's leakage by roughly a third (the NMOS gate current).
            node_of[name] = f"pl_{tag}" if values[name] else "0"
    nodes = []
    for terminal in cell["terminals"]:
        if terminal in node_of:
            nodes.append(node_of[terminal])
        elif terminal == cell["power_pins"]["vdd"]:
            nodes.append(f"pl_{tag}")
        else:
            nodes.append(rail_nodes[terminal])
    return [
        f"Vpl{tag} vdd pl_{tag} DC 0",
        f"X{tag} {' '.join(nodes)} {cell['subckt']}",
    ]


def _rail_nodes(cell: dict[str, Any]) -> dict[str, str]:
    """Map each power terminal of the cell onto the testbench ``vdd``/``0`` net."""
    rail_nodes = {cell["power_pins"]["vdd"]: "vdd", cell["power_pins"]["gnd"]: "0"}
    for key, terminal in cell["power_pins"].items():
        if key not in ("vdd", "gnd"):
            rail_nodes.setdefault(terminal, "vdd" if "v" in key.lower() else "0")
    return rail_nodes


def _full_side_state(
    arc: DerivedArc, cell: dict[str, Any]
) -> tuple[tuple[str, bool], ...]:
    """Every input pin other than the arc's related pin, with the constant it
    is held at.

    Pins the output's ``function`` does not reference (a second output's
    inputs on a multi-output cell, an unused pin) are held **low** -- a
    floating input on a CMOS gate is not a valid operating point, so leaving
    one undriven is never an option.
    """
    derived = arc.side_input_map
    return tuple(
        (pin, derived.get(pin, False))
        for pin in cell["input_pins"]
        if pin != arc.related_pin
    )
