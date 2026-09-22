"""Scoped sub-circuit slicing of a flat extracted netlist -- ``klt extract
--subcircuit <cell>`` (issue #2245).

``klt extract`` is deliberately **flat**: every deck layer is one flattened
``Region``/``Texts`` collection over the selected top cell, so the written
SPICE carries exactly one ``.SUBCKT <top cell>`` body with every recognized
device in it (see ``docs/cli/extract.md``'s "Engine" section). That is the
right default for DRC/LVS/sign-off, but it leaves no way to re-simulate *one
named sub-block* of a routed layout the way a schematic-level campaign
measures it -- a bare ``.subckt delaywin_hv`` deck a testbench instantiates
standalone.

This module closes that gap without touching the flat pass. It runs **after**
the flat extraction (and after any ``--parasitics`` injection) has completed
and the flat netlist has already been written byte-for-byte unchanged, then
*slices* the live ``kdb.Circuit`` in place down to one named sub-cell's own
devices and writes that as a second, independent deck. Nothing here is
reachable unless ``--subcircuit`` was given.

Attribution rule (the whole design in one place)
------------------------------------------------

Devices are attributed to the sub-cell **positionally**, by the same
GDS-level instance path ``devices[].instance_path`` already reports (issue
#1666): a device belongs to the slice when the named cell appears anywhere in
its resolved placement chain.

Nets are then classified from that device attribution, entirely electrically:

``internal``
    Every device terminal on the net belongs to the sub-cell, and the net is
    not an external port (a pin) of the flat deck. It stays an ordinary
    internal node of the emitted ``.SUBCKT``, and **all** of its parasitics
    (per-terminal series legs, the lumped ground capacitor, a distributed
    ladder, a series inductor) are carried over in full.

``boundary``
    The net also carries a terminal of a device *outside* the sub-cell, or it
    is a pin of the flat deck. It is promoted to a ``.SUBCKT`` pin at its
    parasitic **hub** node. The sub-cell keeps the series leg resistance from
    each of its *own* device terminals to that hub -- the share
    ``_terminal_star_weights`` already computed for those terminals -- but the
    net's lumped ground capacitance, its coupling capacitance, and the legs of
    devices outside the sub-cell are **attributed to the parent** and left out.
    That is not an arbitrary coin-flip: a boundary pin is driven by the
    testbench, and a shunt element hung off an ideally-driven node is not
    observable in the measurement, while its series share genuinely is.

``outside``
    No terminal of the sub-cell touches it. Absent from the slice -- unless a
    *kept* parasitic element references it (see below), in which case its hub
    is promoted to a pin so the element has somewhere to attach.

Coupling capacitors are kept whenever **at least one** of the two nets they
span is ``internal``; the far net appears as a pin. Dropping them instead
would silently remove real capacitive load from an internal node and make
post-layout timing optimistic -- the exact "wrong but plausible, passes
tests" outcome this feature has to avoid. A coupling capacitor between two
non-internal nets is dropped (both ends are ideally-driven pins).

The 1 Tohm substrate DC-tie shunt to SPICE's global node ``0`` (issue #1263)
is kept for every substrate net that survives the slice, so the emitted
sub-deck inherits the same no-floating-substrate guarantee the flat deck has.
Node ``0`` itself is never promoted to a pin: it is SPICE's global ground and
already means the same node in every scope.

Because the flat deck is written *unchanged* and the sub-deck is a separate
file, the two are **alternatives, never combined** -- so a parasitic element
that appears in both is not double-counted in any single simulation. Every
element the slice leaves behind *because* it was attributed to the parent is
counted in the response's ``subcircuit.excluded_parasitics`` block rather than
vanishing silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .extract_parasitics import (
    PARASITIC_HUB_PROPERTY,
    PARASITIC_ORIGIN_PROPERTY,
    spice_safe_net_name,
)

if TYPE_CHECKING:
    import klayout.db as kdb


#: Temporary ``kdb.Net`` property key this module uses to resolve an arbitrary
#: ``kdb.Net`` proxy (e.g. one handed back by ``Device.net_for_terminal``) to
#: its index in this module's own net snapshot. Python-side proxy objects are
#: not stable identities and ``Net.cluster_id`` is ``0`` for every
#: manually-created net (verified), so neither can key a dict here. Never
#: observable in output: ``kdb.NetlistSpiceWriter`` has no net-property syntax.
_SLICE_INDEX_PROPERTY = "klt_slice_index"

#: SPICE's global ground node. Duplicated from ``extract._SPICE_GLOBAL_GROUND
#: _NODE`` rather than imported, to keep this module free of a circular import
#: back into ``extract`` (which imports *this* module at call time).
_SPICE_GLOBAL_GROUND = "0"


class SubcircuitSliceError(Exception):
    """A ``--subcircuit`` request that cannot be honoured (the named cell is
    not placed under the top cell, is placed more than once, or contributes no
    recognized device). Raised for the caller (``run_extract``) to re-wrap as
    an ``ExtractError``, so this module stays independent of ``extract``."""


def _device_nets(device: kdb.Device) -> list[kdb.Net]:
    """Every net ``device`` actually connects to, once per connected terminal
    (a terminal left unconnected is skipped, not reported as ``None``)."""
    nets: list[kdb.Net] = []
    for terminal_def in device.device_class().terminal_definitions():
        net = device.net_for_terminal(terminal_def.id())
        if net is not None:
            nets.append(net)
    return nets


def _is_substrate_dc_tie(device: kdb.Device, nets: list[kdb.Net]) -> bool:
    """``True`` for the 1 Tohm substrate DC-tie shunt ``_tie_substrate_nets_
    to_ground`` writes (issue #1263).

    Identified structurally, by the one thing that is unique to it: nothing
    else in an extracted netlist connects a device terminal to SPICE's global
    ground node ``0`` (that net is minted by, and only by, that function).
    """
    return any(net.name == _SPICE_GLOBAL_GROUND for net in nets)


def _parasitic_kind_and_value(device: kdb.Device) -> tuple[str, float]:
    """Classify an injected parasitic element as ``("r", ohms)``,
    ``("c", farads)``, ``("l", henries)`` or ``("", 0.0)``.

    Checked in ``R`` -> ``C`` -> ``L`` order rather than by looking for one
    parameter each, because KLayout's own ``DeviceClassResistor`` declares
    ``R, L, W, A, P`` -- a bare ``has_parameter("L")`` test counts every
    parasitic *resistor* as an inductor too (observed: a 1-resistor exclusion
    reported ``l_count: 1``). Only the *primary* parameter identifies the
    element.
    """
    device_class = device.device_class()
    for name, key in (("R", "r"), ("C", "c"), ("L", "l")):
        if device_class.has_parameter(name):
            return key, float(device.parameter(name))
    return "", 0.0


def selected_device_ids(
    device_instance_paths: dict[int, list[dict[str, Any]]], cell_name: str
) -> frozenset[int]:
    """Every ``Device.id()`` whose resolved GDS instance path (issue #1666)
    descends through a placement of ``cell_name``.

    A device drawn directly in ``cell_name`` -- or in any cell nested below it
    -- matches; a device drawn flat in the top cell (empty path) never does.
    """
    return frozenset(
        device_id
        for device_id, path in device_instance_paths.items()
        if any(level["cell"] == cell_name for level in path)
    )


class _NetIndex:
    """A positional index over one circuit's nets, plus the electrical-net
    identity and parasitic-hub lookup the slice reasons in.

    Exists because neither of the obvious keys works: Python-side ``kdb.Net``
    proxies are not stable identities (two lookups of the same net can hand
    back different objects), and ``Net.cluster_id`` is ``0`` for *every*
    manually-created net -- which is every node ``_inject_parasitics`` mints.
    So each net is tagged with its own index in this snapshot
    (:data:`_SLICE_INDEX_PROPERTY`), and any proxy resolves back through that.

    - ``nets[i]`` -- the net at index ``i``.
    - ``keys[i]`` -- the **electrical net** net ``i`` belongs to. A parasitic
      leg node and the hub it hangs off share one key (the original net's
      ``net_id``), so attribution reasons about real nets rather than about
      ``--parasitics``' internal nodes.
    - ``hub[key]`` -- the index of that electrical net's parasitic hub (the
      net itself when there are no parasitics).
    """

    __slots__ = ("hub", "keys", "nets")

    def __init__(self, circuit: kdb.Circuit) -> None:
        self.nets: list[kdb.Net] = list(circuit.each_net())
        for index, net in enumerate(self.nets):
            net.set_property(_SLICE_INDEX_PROPERTY, index)

        self.keys: list[tuple[str, int]] = []
        self.hub: dict[tuple[str, int], int] = {}
        for index, net in enumerate(self.nets):
            origin = net.property(PARASITIC_ORIGIN_PROPERTY)
            if origin is not None:
                key = ("o", int(origin))
                if net.property(PARASITIC_HUB_PROPERTY) is not None:
                    self.hub[key] = index
            elif net.cluster_id:
                key = ("c", int(net.cluster_id))
                self.hub[key] = index
            else:
                key = ("i", index)
                self.hub[key] = index
            self.keys.append(key)
        # Defensive: an origin with no tagged hub cannot happen
        # (`_inject_parasitics` always tags one), but fall back to the net
        # itself rather than raising a KeyError deep in the slice.
        for index, key in enumerate(self.keys):
            self.hub.setdefault(key, index)

    def index_of(self, net: kdb.Net) -> int:
        return int(net.property(_SLICE_INDEX_PROPERTY))

    def key_of(self, net: kdb.Net) -> tuple[str, int]:
        return self.keys[self.index_of(net)]

    def hub_of(self, index: int) -> int:
        return self.hub[self.keys[index]]


class _Attribution:
    """Which electrical nets are ``internal`` to the sub-cell, which are
    ``boundary`` (also reached from outside, or an external port of the flat
    deck), and which net indices carry one of the sub-cell's own device
    terminals. See this module's docstring for the classification."""

    __slots__ = ("boundary", "internal", "selected_net_indices")

    def __init__(
        self,
        circuit: kdb.Circuit,
        index: _NetIndex,
        devices: list[kdb.Device],
        *,
        selected_ids: frozenset[int],
        pre_parasitic_device_ids: frozenset[int],
    ) -> None:
        inside: set[tuple[str, int]] = set()
        outside: set[tuple[str, int]] = set()
        self.selected_net_indices: set[int] = set()
        for device in devices:
            if device.id() not in pre_parasitic_device_ids:
                continue  # injected parasitic element, not a real terminal
            selected = device.id() in selected_ids
            for net in _device_nets(device):
                if selected:
                    inside.add(index.key_of(net))
                    self.selected_net_indices.add(index.index_of(net))
                else:
                    outside.add(index.key_of(net))
        # An external port of the flat deck crosses the sub-cell boundary by
        # definition -- the parent (or the testbench) drives it -- so it counts
        # as touched from outside even with no outside device terminal on it.
        # This is also what keeps a `--pins`/`--def-pins` declared port a pin
        # of the slice.
        for pin in circuit.each_pin():
            pin_net = circuit.net_for_pin(pin.id())
            if pin_net is not None:
                outside.add(index.key_of(pin_net))

        self.internal = inside - outside
        self.boundary = inside & outside


def _is_boundary_leg(
    index: _NetIndex, attribution: _Attribution, device_nets: list[kdb.Net]
) -> bool:
    """``True`` for an injected series element bridging one of the sub-cell's
    own device terminals to a boundary net's hub -- the one parasitic on a
    boundary net the slice keeps (see this module's docstring)."""
    if len(device_nets) != 2:
        return False
    first, second = (index.index_of(net) for net in device_nets)
    key = index.keys[first]
    if key != index.keys[second] or key not in attribution.boundary:
        return False
    hub = index.hub[key]
    for leg, other in ((first, second), (second, first)):
        if other == hub and leg != hub and leg in attribution.selected_net_indices:
            return True
    return False


class _Selection:
    """The keep/drop decision for every device in the circuit, plus the
    per-parasitic accounting for what the boundary rule handed to the parent.

    ``substrate_tie_ids`` is decided *last* (in :func:`slice_subcircuit`): the
    1 Tohm DC tie to SPICE node ``0`` is kept iff the net it ties survives,
    which is only known once every other device has been decided.
    """

    __slots__ = ("device_nets", "excluded", "kept_ids", "substrate_tie_ids")

    def __init__(
        self,
        index: _NetIndex,
        attribution: _Attribution,
        devices: list[kdb.Device],
        *,
        selected_ids: frozenset[int],
        pre_parasitic_device_ids: frozenset[int],
    ) -> None:
        self.kept_ids: set[int] = set()
        self.substrate_tie_ids: set[int] = set()
        self.device_nets: dict[int, list[kdb.Net]] = {}
        self.excluded = {
            "r_count": 0,
            "c_count": 0,
            "l_count": 0,
            "resistance_ohm": 0.0,
            "capacitance_ff": 0.0,
        }
        for device in devices:
            device_id = device.id()
            device_nets = _device_nets(device)
            self.device_nets[device_id] = device_nets
            if device_id in pre_parasitic_device_ids:
                if device_id in selected_ids:
                    self.kept_ids.add(device_id)
                continue
            if _is_substrate_dc_tie(device, device_nets):
                self.substrate_tie_ids.add(device_id)
                continue
            device_keys = {index.key_of(net) for net in device_nets}
            if device_keys & attribution.internal or _is_boundary_leg(
                index, attribution, device_nets
            ):
                self.kept_ids.add(device_id)
            elif device_keys & attribution.boundary:
                self._account_excluded(device)

    def _account_excluded(self, device: kdb.Device) -> None:
        """Record one parasitic element the boundary rule attributed to the
        parent deck, so it is accounted for rather than silently dropped."""
        kind, value = _parasitic_kind_and_value(device)
        if kind == "r":
            self.excluded["r_count"] += 1
            self.excluded["resistance_ohm"] += value
        elif kind == "c":
            self.excluded["c_count"] += 1
            self.excluded["capacitance_ff"] += value * 1e15
        elif kind == "l":
            self.excluded["l_count"] += 1


def _reconnect_orphaned_boundary_legs(
    index: _NetIndex,
    attribution: _Attribution,
    selection: _Selection,
    devices: list[kdb.Device],
    *,
    selected_ids: frozenset[int],
) -> set[str]:
    """Move any of the sub-cell's own terminals that sit on a boundary net's
    *leg* with no kept series element bridging it to the hub straight onto the
    hub (the pin node), and return the affected hubs' net names.

    Reached by a ``--distributed-rc`` ladder, whose segments chain adjacent
    legs rather than each reaching the hub. Without this the leg would survive
    as a dangling node with nothing but one device terminal on it.
    """
    kept_links: set[tuple[int, int]] = set()
    for device_id in selection.kept_ids:
        device_nets = selection.device_nets[device_id]
        if len(device_nets) != 2:
            continue
        first, second = (index.index_of(net) for net in device_nets)
        kept_links.add((first, second))
        kept_links.add((second, first))

    reconnected: set[str] = set()
    for device in devices:
        if device.id() not in selected_ids:
            continue
        for terminal_def in device.device_class().terminal_definitions():
            net = device.net_for_terminal(terminal_def.id())
            if net is None:
                continue
            position = index.index_of(net)
            if index.keys[position] not in attribution.boundary:
                continue
            hub = index.hub_of(position)
            if position == hub or (position, hub) in kept_links:
                continue
            device.disconnect_terminal(terminal_def.id())
            device.connect_terminal(terminal_def.id(), index.nets[hub])
            reconnected.add(index.nets[hub].expanded_name())
    return reconnected


def _clear_pins(circuit: kdb.Circuit, cell_name: str) -> None:
    """Remove every pin the flat deck declared.

    The flat deck's pin interface is the *top cell's*, not this sub-cell's, so
    all of it goes; the slice declares its own afterwards. Drained
    one-at-a-time rather than by iterating a snapshot and removing each id, so
    the loop cannot depend on whether ``remove_pin`` reindexes the survivors
    (it does not today -- but a snapshot-plus-id loop would silently leave
    stale pins in the emitted ``.SUBCKT`` header if it ever did). Bounded by
    the starting count, so a hypothetical no-op removal raises instead of
    spinning.
    """
    for _ in range(circuit.pin_count()):
        if circuit.pin_count() == 0:
            break
        circuit.remove_pin(next(iter(circuit.each_pin())).id())
    if circuit.pin_count() != 0:
        raise SubcircuitSliceError(
            f"--subcircuit {cell_name!r}: could not clear the flat deck's own "
            f"pin interface ({circuit.pin_count()} pin(s) left) -- refusing to "
            "emit a .SUBCKT whose header mixes top-cell ports with the "
            "sub-cell's own"
        )


def _declare_pins(
    circuit: kdb.Circuit,
    index: _NetIndex,
    attribution: _Attribution,
    kept_net_indices: set[int],
) -> tuple[list[dict[str, str]], list[str]]:
    """Declare the slice's own pins, and return ``(pins[], internal named
    nets)``.

    A kept net becomes a pin unless it is genuinely internal to the sub-cell:
    an ``internal``-key net, a per-terminal leg of a boundary net, or SPICE's
    global ground node ``0`` (same node in every scope already, so exposing it
    as a formal argument would only let a testbench rebind it by mistake).
    """
    candidates: list[tuple[str, str, int]] = []
    internal_named: list[str] = []
    for position in sorted(kept_net_indices):
        net = index.nets[position]
        key = index.keys[position]
        if net.name == _SPICE_GLOBAL_GROUND:
            continue
        if key in attribution.internal:
            # Only the hub carries the *layout* label: a per-terminal leg is a
            # synthesized `<label>__t<n>` node, not a port a caller could have
            # expected to drive, so reporting it would be pure noise.
            if net.name and position == index.hub[key]:
                internal_named.append(spice_safe_net_name(net.expanded_name()))
            continue
        if position != index.hub[key]:
            continue
        # `boundary`: a net the sub-cell's own devices sit on that also leaves
        # it (or is an external port of the flat deck) -- a real port of the
        # block. `parasitic`: a net only a kept parasitic element reaches (the
        # substrate/ground reference a ground capacitor hangs off, or the far
        # side of a boundary-crossing coupling capacitor), exposed so the
        # testbench can terminate it -- tying it to a quiet rail is the
        # conservative default.
        role = "boundary" if key in attribution.boundary else "parasitic"
        candidates.append((role, spice_safe_net_name(net.expanded_name()), position))
    # Deterministic pin order: real ports first, then the parasitic references,
    # each alphabetically. This is the order the .SUBCKT header (and the
    # reported `instance_line`) declares, so a committed testbench stays valid.
    candidates.sort(key=lambda entry: (entry[0] != "boundary", entry[1], entry[2]))
    for _role, name, position in candidates:
        pin = circuit.create_pin(name)
        circuit.connect_pin(pin.id(), index.nets[position])
    pins = [{"name": name, "role": role} for role, name, _position in candidates]
    return pins, internal_named


def _resolve_kept_nets(
    index: _NetIndex,
    selection: _Selection,
    devices: list[kdb.Device],
    *,
    selected_ids: frozenset[int],
) -> set[int]:
    """Every net index the sliced circuit keeps, and the final keep decision
    for the substrate DC ties.

    The selected devices' terminals are re-read (rather than taken from
    ``selection.device_nets``) because
    :func:`_reconnect_orphaned_boundary_legs` may have moved one onto a hub
    that no kept device otherwise references. The 1 Tohm substrate DC tie
    (issue #1263) then rides along for every substrate net that survived, so
    the sub-deck inherits the same no-floating-substrate guarantee the flat
    deck has -- decided last, because it depends on that final net set.
    """
    kept: set[int] = set()
    for device_id in selection.kept_ids:
        if device_id in selected_ids:
            # Re-read from the live device below instead of the snapshot:
            # `_reconnect_orphaned_boundary_legs` may have moved one of this
            # device's terminals off an orphaned boundary leg on to the hub,
            # and the pre-reconnect snapshot would otherwise keep that
            # now-terminal-less, pin-less leg alive -- invisible in the
            # emitted SPICE but counted by `net_count`, which the docs define
            # as "what the emitted .SUBCKT actually carries".
            continue
        for net in selection.device_nets[device_id]:
            kept.add(index.index_of(net))
    for device in devices:
        if device.id() in selected_ids:
            for net in _device_nets(device):
                kept.add(index.index_of(net))
    for device_id in selection.substrate_tie_ids:
        tie_nets = selection.device_nets[device_id]
        tied = [net for net in tie_nets if net.name != _SPICE_GLOBAL_GROUND]
        if any(index.index_of(net) in kept for net in tied):
            selection.kept_ids.add(device_id)
            for net in tie_nets:
                kept.add(index.index_of(net))
    return kept


def _rename_and_isolate(
    circuit: kdb.Circuit, flat_circuit_name: str, cell_name: str
) -> None:
    """Rename the sliced circuit to ``cell_name`` and drop every sibling
    circuit from its netlist.

    The sliced circuit is the *only* thing the sub-deck should carry --
    ``Netlist.write`` would otherwise emit a sibling as a second ``.SUBCKT``
    alongside the slice. (Unreachable today: ``--abstract-cells``, the one mode
    that adds sibling circuits, is rejected before this runs. Defensive, not
    dead.)
    """
    netlist = circuit.netlist()
    for other in list(netlist.each_circuit()):
        if other.name != flat_circuit_name:
            netlist.remove(other)
    circuit.name = cell_name


def slice_subcircuit(
    circuit: kdb.Circuit,
    *,
    cell_name: str,
    selected_ids: frozenset[int],
    pre_parasitic_device_ids: frozenset[int],
) -> dict[str, Any]:
    """Slice ``circuit`` **in place** down to ``cell_name``'s own devices,
    turning it into a standalone ``.SUBCKT`` ready to be written.

    ``selected_ids`` is the set of ``Device.id()`` attributed to the sub-cell
    (see :func:`selected_device_ids`); ``pre_parasitic_device_ids`` is the set
    of device ids that existed *before* any ``--parasitics`` injection, i.e.
    the real extracted devices -- everything else in ``circuit`` is an injected
    parasitic R/C/L. Both are ``Device.id()``-keyed because that id is stable
    across the whole pipeline (the same reason ``device_instance_paths`` is).

    Returns the response's ``subcircuit`` block minus the two fields only the
    caller can fill in (``path``/``sha256``). Raises
    :class:`SubcircuitSliceError` when the request cannot be honoured.

    The caller must have already written the flat netlist: this mutates the
    live circuit destructively (pins, devices and nets are removed) and there
    is no way back.
    """
    if not selected_ids:
        raise SubcircuitSliceError(
            f"--subcircuit {cell_name!r} matched no extracted device -- the "
            "cell is placed in the layout hierarchy but none of the devices "
            "this deck recognized resolve to a placement of it (check the "
            "cell actually draws device-recognition geometry, and see "
            "devices[].instance_path in a plain `klt extract --format json` "
            "run for what each device was attributed to)"
        )
    if any(True for _ in circuit.each_subcircuit()):
        raise SubcircuitSliceError(
            f"--subcircuit {cell_name!r} cannot slice a netlist that already "
            "carries subcircuit instances -- an instance has no position to "
            "attribute it to a sub-cell with (unlike a device, issue #1666). "
            "Re-run without --abstract-cells."
        )

    warnings: list[str] = []
    flat_circuit_name = circuit.name
    devices = list(circuit.each_device())

    index = _NetIndex(circuit)
    attribution = _Attribution(
        circuit,
        index,
        devices,
        selected_ids=selected_ids,
        pre_parasitic_device_ids=pre_parasitic_device_ids,
    )
    selection = _Selection(
        index,
        attribution,
        devices,
        selected_ids=selected_ids,
        pre_parasitic_device_ids=pre_parasitic_device_ids,
    )
    reconnected = _reconnect_orphaned_boundary_legs(
        index, attribution, selection, devices, selected_ids=selected_ids
    )
    if reconnected:
        names = ", ".join(
            repr(spice_safe_net_name(name)) for name in sorted(reconnected)
        )
        warnings.append(
            f"--subcircuit {cell_name!r}: boundary net(s) {names} carry a "
            "multi-segment (distributed) parasitic RC ladder that no single "
            "segment bridges to the pin node -- the sub-cell's own terminals "
            "were connected directly to the pin instead, so none of that net's "
            "series resistance is inside the emitted .SUBCKT. See "
            "docs/cli/extract.md's 'Sub-circuit isolation' section."
        )

    # ------------------------------------------------------- apply the cut --
    # Pins first (a net still bound to a pin cannot be removed), then devices,
    # then the nets nothing kept references.
    _clear_pins(circuit, cell_name)

    kept_net_indices = _resolve_kept_nets(
        index, selection, devices, selected_ids=selected_ids
    )

    for device in devices:
        if device.id() not in selection.kept_ids:
            circuit.remove_device(device)
    for position, net in enumerate(index.nets):
        if position not in kept_net_indices:
            circuit.remove_net(net)

    pins, internal_named = _declare_pins(circuit, index, attribution, kept_net_indices)
    if internal_named:
        names = ", ".join(repr(name) for name in sorted(internal_named))
        warnings.append(
            f"--subcircuit {cell_name!r}: net(s) {names} carry a layout label "
            "but stay internal nodes of the emitted .SUBCKT -- nothing outside "
            "the sub-cell connects to them, so they are not ports. Draw the "
            "label in the top cell (or declare it with --pins) if a testbench "
            "needs to drive or probe it. See docs/cli/extract.md's "
            "'Sub-circuit isolation' section."
        )

    _rename_and_isolate(circuit, flat_circuit_name, cell_name)

    excluded = selection.excluded
    return {
        "cell": cell_name,
        "pins": pins,
        "device_count": sum(1 for _ in circuit.each_device()),
        "net_count": sum(1 for _ in circuit.each_net()),
        # Ready to paste into a testbench: the `X` card that instantiates the
        # emitted `.SUBCKT` with its pins in the order they were declared.
        "instance_line": " ".join(
            ["XDUT", *(entry["name"] for entry in pins), cell_name]
        ),
        # Every parasitic element the boundary rule attributed to the parent
        # deck rather than to this sub-circuit (see this module's docstring) --
        # accounted for here so nothing vanishes silently.
        "excluded_parasitics": {
            "r_count": excluded["r_count"],
            "c_count": excluded["c_count"],
            "l_count": excluded["l_count"],
            "resistance_ohm": round(excluded["resistance_ohm"], 4),
            "capacitance_ff": round(excluded["capacitance_ff"], 6),
        },
        "warnings": warnings,
    }


def cell_placement_count(layout: kdb.Layout, top_cell: kdb.Cell, cell_name: str) -> int:
    """How many times ``cell_name`` is placed under ``top_cell``, counting
    every element of every ``CellInstArray`` separately.

    ``0`` means the cell is not in the layout at all, or is in the layout but
    not reachable from ``top_cell``. Used to reject a ``--subcircuit`` request
    whose named cell is placed more than once: ``devices[].instance_path``
    records cell *names*, not per-placement identities, so two sibling
    placements of the same cell are indistinguishable and a slice would
    silently merge both copies' devices into one ``.SUBCKT``.
    """
    target = layout.cell(cell_name)
    if target is None:
        return 0
    target_index = target.cell_index()
    memo: dict[int, int] = {}

    def count_under(cell_index: int) -> int:
        if cell_index == target_index:
            return 1
        cached = memo.get(cell_index)
        if cached is not None:
            return cached
        total = 0
        for inst in layout.cell(cell_index).each_inst():
            per_element = count_under(inst.cell_index)
            if per_element:
                total += per_element * inst.size()
        memo[cell_index] = total
        return total

    return count_under(top_cell.cell_index())
