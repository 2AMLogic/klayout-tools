"""A plain, JSON-serialisable digest of a parsed SPICE netlist (issue #1130,
Phase A of ``docs/design/netlist-driven-layout-spike.md``).

**The gap this closes.** ``klt lvs``'s ``lvs.py`` already has a real SPICE
*parser* -- ``_read_reference_netlist`` constructs a ``kdb.Netlist`` via
``kdb.NetlistSpiceReader``, handling both the plain-element form and, via
:mod:`klayout_tools.netlist_normalize`, the subcircuit-call (simulation) form
real open-PDK schematic flows emit. But every consumer of that parse today
has to import ``klayout.db`` directly and walk ``kdb.Circuit``/``kdb.Device``/
``kdb.Net`` SWIG objects by hand -- correct for ``lvs.py``'s own in-process
comparison, but not a caller-facing contract (the objects are not JSON; there
is no ``docs/schemas/*netlist*`` equivalent). This module is the thin adapter
the spike proposes: walk the parsed netlist *once* into a plain Python dict
of JSON-serialisable primitives, so nothing downstream of this module needs
to import ``klayout.db`` at all to read a device/net list.

**Reuses, never reimplements, the existing parse.** :func:`build_netlist_digest`
calls ``lvs.py``'s own ``_read_reference_netlist``/``_select_circuit`` --
the same private helpers ``klt lvs``'s reference-side request already
resolves through (``path``/``form``/``deck``/``device_map``, the identical
keyword shape) -- rather than opening a second, independent SPICE-reading
code path. Importing a leading-underscore name from a sibling module in this
package is an established convention here (see e.g. ``sim.py``'s
``from .pdk_models import _pdk_variant_family``), not a layering violation.

**Shape.** ``devices[]``: ``{"name", "device_class", "terminals":
{<terminal-name>: <net-name-or-None>}, "params": {<param-name>: <value>}}``.
``nets[]``: ``{"name", "is_pin", "terminals": [{"device", "terminal"}, ...]}``.
Both lists are sorted (devices by name, nets by name, and each net's own
``terminals[]`` by ``(device, terminal)``) so the digest is deterministic --
diffable and directly comparable across runs, per this issue's acceptance
criteria. Net names pass through ``extract.py``'s
:func:`~klayout_tools.extract.spice_safe_net_name` (issue #696), the same
comma-to-pipe rewrite ``klt lvs``'s own ``net_correspondence``/
``mismatches[].net`` already apply, so a label-merged net's name is
byte-identical to how the rest of this project's JSON contract spells it.

**Device ``name`` is per-class, not circuit-global.** ``KLayout``'s own
``NetlistSpiceReader`` always strips the leading SPICE element-type letter
from an instance token when it builds ``Device.expanded_name()`` (``"M1"``
-> ``"1"``, ``"R1"`` -> ``"1"``) -- confirmed against the installed
``klayout.db`` module, and already the established convention throughout
this project's own JSON contract (``docs/cli/lvs.md``'s
``mismatches[].device`` reports the same bare, letter-stripped names, e.g.
``{"layout": "2", "class": "PFET"}``). A real schematic's ordinary SPICE
naming convention (a separate ``M``/``R``/``C``/``Q`` counter per element
type) therefore makes ``"1"`` a *legitimate*, simultaneous name for one MOS
device, one resistor, and one capacitor in the same circuit -- ``name``
alone does not uniquely identify a device the way it would in the original
SPICE text; pair it with ``device_class`` (as ``klt lvs`` itself already
does) to disambiguate.

No CLI subcommand is added by this module (deliberately -- see the issue);
:func:`build_netlist_digest` is a library-level function only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .extract import spice_safe_net_name

if TYPE_CHECKING:
    import klayout.db as kdb


def _net_digest_name(net: kdb.Net) -> str:
    return spice_safe_net_name(net.expanded_name())


def _device_digest(device: kdb.Device) -> dict[str, Any]:
    device_class = device.device_class()

    terminals: dict[str, str | None] = {}
    for terminal_def in device_class.terminal_definitions():
        net = device.net_for_terminal(terminal_def.id())
        terminals[terminal_def.name] = (
            _net_digest_name(net) if net is not None else None
        )

    params: dict[str, float] = {}
    for param_def in device_class.parameter_definitions():
        params[param_def.name] = device.parameter(param_def.id())

    return {
        "name": device.expanded_name(),
        "device_class": device_class.name,
        "terminals": terminals,
        "params": params,
    }


def _net_digest(net: kdb.Net) -> dict[str, Any]:
    terminals = [
        {
            "device": terminal_ref.device().expanded_name(),
            "terminal": terminal_ref.terminal_def().name,
        }
        for terminal_ref in net.each_terminal()
    ]
    terminals.sort(key=lambda entry: (entry["device"], entry["terminal"]))

    return {
        "name": _net_digest_name(net),
        "is_pin": net.pin_count() > 0,
        "terminals": terminals,
    }


def build_netlist_digest(
    path: str,
    *,
    top: str | None = None,
    form: str = "plain-element",
    deck: str | None = None,
    device_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Parse the SPICE netlist at ``path`` and return a plain,
    JSON-serialisable digest of one circuit's devices and nets.

    ``top``/``form``/``deck``/``device_map`` are exactly the fields ``klt
    lvs``'s ``request.reference`` shape already accepts
    (``docs/cli/lvs.md``): ``form="plain-element"`` (default) reads the file
    as-is; ``form="subckt-call"`` first converts a real PDK schematic flow's
    simulation-form SPICE (MOS *and*, per issue #1130,
    resistor/capacitor/bipolar) to the plain-element form via
    :mod:`klayout_tools.netlist_normalize`, resolving device names through
    ``deck``/``device_map`` the same way ``klt lvs`` does. ``top`` selects
    the circuit to digest by name; omitted, the netlist's sole top circuit
    is used (an ambiguous or missing top circuit is an
    :class:`~klayout_tools.lvs.LvsError`, the same error ``klt lvs`` itself
    raises for this case -- this module reuses that check rather than
    reimplementing it).

    Returns ``{"circuit": <name>, "devices": [...], "nets": [...]}`` -- see
    the module docstring for the exact per-entry shape. A circuit with zero
    devices of a given family (or zero devices/nets at all) digests to an
    empty list for that field, never an error; an unresolvable device name
    in a ``form="subckt-call"`` netlist raises the same
    :class:`~klayout_tools.netlist_normalize.NormalizeError`-wrapped
    :class:`~klayout_tools.lvs.LvsError` ``_read_reference_netlist`` itself
    raises for that case -- no separate, weaker error handling here.
    """
    from .lvs import _read_reference_netlist, _select_circuit

    netlist = _read_reference_netlist(path, form=form, deck=deck, device_map=device_map)
    circuit = _select_circuit(netlist, top, "netlist")

    devices = sorted(
        (_device_digest(device) for device in circuit.each_device()),
        # `(name, device_class)`, not `name` alone -- a device's bare
        # `name` is only unique *within* its own class (see the module
        # docstring's "Device `name` is per-class" note), so the class is
        # part of the sort key too, for a fully deterministic order even
        # when two different-class devices share a name.
        key=lambda entry: (entry["name"], entry["device_class"]),
    )
    nets = sorted(
        (_net_digest(net) for net in circuit.each_net()),
        key=lambda entry: entry["name"],
    )

    return {"circuit": circuit.name, "devices": devices, "nets": nets}
