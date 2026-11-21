"""Opt-in hierarchical SPICE emission for ``klt extract --hierarchical-cells``
(issue #2722 increment 1; nested selections, issue #2916).

``klt extract`` is flat by default: every deck layer is one flattened
``Region`` over the top cell, so the written SPICE carries a single
``.SUBCKT <top>`` holding every device (see ``docs/cli/extract.md``'s "Engine"
section). That leaves ``klt lvs``'s per-circuit options (for example
``options.combine_devices_per_circuit``) nothing to key on on the layout side
of a composed layout.

This module rebuilds hierarchy *in the extracted* ``kdb.Netlist`` -- after
the flat extraction and before the ordinary ``NetlistSpiceWriter`` runs -- so
the existing writer, model-binding delegate, net-name handling and digest path
stay authoritative. Nothing here is reachable unless the option was given.

Contract
--------

* The caller names cell definitions. One ``.SUBCKT`` is emitted per selected
  *cell definition*; every placement of it becomes one ``X`` instance in the
  circuit that holds it: the top circuit, or -- when the placement sits
  inside a placement of another selected cell -- that cell's ``.SUBCKT``
  (nested selections, issue #2916). A device belongs to exactly one circuit:
  the innermost selected placement it is drawn in.
* Devices are attributed to a *placement* positionally, by the same
  ``begin_instances_rec_touching`` query ``devices[].instance_path`` uses
  (issue #1666), but keyed on the placement's accumulated transform rather
  than only the cell name, so two sibling placements stay distinguishable.
* A child net becomes a ``.SUBCKT`` pin when, in at least one placement, it
  also carries a terminal of a device outside that placement's whole subtree
  (nested placements included) or is a pin of the flat deck. In placements
  where it is internal the pin is still wired
  (to a parent net nothing else touches), so every placement of one cell
  instantiates one identical definition.
* Placements of one cell must be electrically identical (same devices at the
  same cell-local positions, same parameters, same net partition); anything
  else is an error rather than a silently merged or wrong definition.
* Conservation is verified before the netlist is handed back: every
  pre-transform device appears exactly once (per-class counts too),
  counting each definition once per instance path from the top, so it holds
  across every nesting level.

``--parasitics``, ``--subcircuit`` and ``--abstract-cells`` stay rejected
with an error naming the combination and the reason (decision recorded in
issue #2916; see :func:`reject_unsupported_combinations`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .extract_parasitics import spice_safe_net_name
from .pdk_models import MOS_FLAVOUR_PROPERTY

if TYPE_CHECKING:
    import klayout.db as kdb

#: Follow-up issue that owns every deferred shape; cited in every rejection.
FOLLOW_UP_ISSUE = "#2916"

#: Temporary ``kdb.Net`` property key mapping a net proxy back to its index in
#: this module's snapshot (Python-side ``kdb.Net`` proxies are not stable
#: identities). Never observable in output.
_NET_INDEX_PROPERTY = "klt_hier_index"

_PARAMETER_REL_TOL = 1e-6


class HierarchyError(Exception):
    """A ``--hierarchical-cells`` request that cannot be honoured. Re-wrapped
    as an ``ExtractError`` by ``run_extract`` so this module stays independent
    of ``extract``."""


_COMBINATION_REASONS = {
    "--parasitics": (
        "parasitic elements are extracted on the flat deck against flat "
        "nets, and a net that crosses a .SUBCKT boundary has no single "
        "owning circuit, so splitting them across definitions would drop or "
        "double count boundary and coupling elements"
    ),
    "--subcircuit": (
        "--subcircuit writes a second, flat slice of one cell from the flat "
        "deck, which would compete with the hierarchy this option emits"
    ),
    "--abstract-cells": (
        "abstracted cells are erased before extraction, so the placements "
        "this option attributes devices to no longer exist"
    ),
}
_COMBINATION_REASONS["--abstract-cell-lef"] = _COMBINATION_REASONS["--abstract-cells"]


def reject_unsupported_combinations(
    *,
    parasitics: bool,
    subcircuit_cell: str | None,
    abstract_cell_patterns: tuple[str, ...],
    abstract_cell_lef_paths: tuple[str, ...] = (),
) -> None:
    """Raise for every option combination that stays unsupported, naming the
    combination and why (decision recorded in issue #2916)."""
    combos = (
        ("--parasitics", parasitics),
        ("--subcircuit", subcircuit_cell is not None),
        ("--abstract-cells", bool(abstract_cell_patterns)),
        ("--abstract-cell-lef", bool(abstract_cell_lef_paths)),
    )
    for flag, given in combos:
        if given:
            raise HierarchyError(
                f"--hierarchical-cells cannot be combined with {flag}: "
                f"{_COMBINATION_REASONS[flag]} (decision recorded in "
                f"{FOLLOW_UP_ISSUE}). Run the two modes as separate "
                "extractions"
            )


def validate_selection(
    layout: kdb.Layout, top_cell: kdb.Cell, names: tuple[str, ...]
) -> None:
    """Reject, before anything is written, a selection that is empty or names
    an absent / top / unreachable cell. Nested pairs are supported."""
    if not names:
        raise HierarchyError("--hierarchical-cells was given no cell name")
    top_called = {top_cell.cell_index(), *top_cell.called_cells()}
    for name in names:
        cell = layout.cell(name)
        if cell is None:
            raise HierarchyError(
                f"--hierarchical-cells {name!r} names no cell in this layout"
            )
        if cell.cell_index() == top_cell.cell_index():
            raise HierarchyError(
                f"--hierarchical-cells {name!r} names the top cell itself -- "
                "the top circuit is always emitted; pass cells instantiated "
                "*below* the top cell"
            )
        if cell.cell_index() not in top_called:
            raise HierarchyError(
                f"--hierarchical-cells {name!r} has no reachable placement "
                f"under top cell {top_cell.name!r} -- pass the name of a "
                "cell instantiated in this layout's hierarchy"
            )


@dataclass(frozen=True)
class Placement:
    """One placement of a selected cell, identified by accumulated transform."""

    cell: str
    key: tuple[float, ...]
    #: local (cell-frame) position of the device, rounded to the nm grid
    local: tuple[int, int]


def device_placements(
    top_cell: kdb.Cell,
    circuit: kdb.Circuit,
    selected: frozenset[str],
    dbu: float,
) -> dict[int, tuple[Placement, ...]]:
    """``Device.id()`` -> the chain of :class:`Placement` s of selected cells
    the device was drawn in, outermost first (several entries when selected
    cells nest, issue #2916). Devices outside every selected cell are absent.

    Ties between overlapping candidate chains are broken exactly as
    ``extract._instance_path_for_point`` does (deepest chain, then smallest
    matched area, then ``(cell, ia, ib)`` per level) so this attribution
    agrees with ``devices[].instance_path``.
    """
    import klayout.db as kdb

    result: dict[int, tuple[Placement, ...]] = {}
    for device in circuit.each_device():
        disp = device.trans.disp
        point = kdb.DPoint(disp.x, disp.y)
        iterator = top_cell.begin_instances_rec_touching(
            kdb.DBox(point.x, point.y, point.x, point.y)
        )
        best_key: tuple[Any, ...] | None = None
        best_elements: list[Any] = []
        while not iterator.at_end():
            elements = list(iterator.path())
            elements.append(iterator.current_inst_element())
            key_parts = tuple(
                (element.inst().cell.name, element.ia(), element.ib())
                for element in elements
            )
            area = iterator.inst_cell().dbbox().transformed(iterator.dtrans()).area()
            key = (len(elements), -area, key_parts)
            if best_key is None or key > best_key:
                best_key = key
                best_elements = elements
            iterator.next()

        cumulative = kdb.DCplxTrans()
        chain: list[Placement] = []
        for element in best_elements:
            step = element.specific_cplx_trans()
            cumulative = cumulative * kdb.DCplxTrans(
                step.mag,
                step.angle,
                step.is_mirror(),
                step.disp.x * dbu,
                step.disp.y * dbu,
            )
            name = element.inst().cell.name
            if name in selected:
                local = cumulative.inverted() * point
                chain.append(
                    Placement(
                        cell=name,
                        key=(
                            round(cumulative.disp.x * 1000),
                            round(cumulative.disp.y * 1000),
                            round(cumulative.angle * 1000),
                            1 if cumulative.is_mirror() else 0,
                            round(cumulative.mag * 1e6),
                        ),
                        local=(round(local.x * 1000), round(local.y * 1000)),
                    )
                )
        if chain:
            result[device.id()] = tuple(chain)
    return result


# --------------------------------------------------------------------------- #
# Netlist transformation
# --------------------------------------------------------------------------- #


def _terminal_nets(device: kdb.Device) -> list[tuple[int, int]]:
    """``(terminal id, net snapshot index)`` for every connected terminal."""
    pairs: list[tuple[int, int]] = []
    for terminal_def in device.device_class().terminal_definitions():
        net = device.net_for_terminal(terminal_def.id())
        if net is not None:
            pairs.append((terminal_def.id(), int(net.property(_NET_INDEX_PROPERTY))))
    return pairs


def _parameters(device: kdb.Device) -> dict[str, float]:
    return {
        pd.name: float(device.parameter(pd.id()))
        for pd in device.device_class().parameter_definitions()
    }


class _PlacementView:
    """One placement's devices and the nets they touch, in cell-local terms."""

    __slots__ = (
        "cell",
        "children",
        "device_keys",
        "devices",
        "direct_keys",
        "key",
        "nets_by_sig",
        "parent",
    )

    def __init__(self, cell: str, key: tuple[float, ...]) -> None:
        self.cell = cell
        self.key = key
        #: the enclosing selected placement, ``None`` for a top-level one
        self.parent: _PlacementView | None = None
        #: selected placements directly inside this one
        self.children: list[_PlacementView] = []
        #: local keys of devices drawn in this placement but in no nested one
        self.direct_keys: set[tuple[str, int, int]] = set()
        #: local key -> device (the whole subtree, nested placements included)
        self.devices: dict[tuple[str, int, int], kdb.Device] = {}
        self.device_keys: list[tuple[str, int, int]] = []
        #: signature -> (net index, is boundary)
        self.nets_by_sig: dict[frozenset, tuple[int, bool]] = {}


@dataclass
class _InstancePlan:
    """One ``X`` instance to create in the parent."""

    child: kdb.Circuit
    name: str
    view: _PlacementView
    pin_sigs: list[frozenset]


def build_hierarchy(
    netlist: kdb.Netlist,
    top_circuit: kdb.Circuit,
    placements: dict[int, tuple[Placement, ...]],
) -> dict[str, Any]:
    """Rebuild ``netlist`` in place into one ``.SUBCKT`` per selected cell
    plus the top circuit instantiating them, and return the response's
    ``hierarchy`` block.

    Mutates ``top_circuit``/``netlist`` destructively: call only once every
    flat-view report field has been computed and before the writer runs.
    Raises :class:`HierarchyError` (before anything is written) when a cell's
    placements are not identical or the conservation check fails.
    """
    if any(True for _ in top_circuit.each_subcircuit()):
        raise HierarchyError(
            "--hierarchical-cells cannot restructure a netlist that already "
            "carries subcircuit instances"
        )
    devices = list(top_circuit.each_device())
    nets = list(top_circuit.each_net())
    for index, net in enumerate(nets):
        net.set_property(_NET_INDEX_PROPERTY, index)
    pre_class_counts = _class_counts(devices)

    views = _group_by_placement(devices, placements)
    _annotate_views(top_circuit, devices, placements, views)

    by_cell: dict[str, list[_PlacementView]] = {}
    for (cell, _key), view in sorted(views.items()):
        by_cell.setdefault(cell, []).append(view)
    taken_names = {circuit.name for circuit in netlist.each_circuit()}
    for cell in by_cell:
        if cell in taken_names:
            raise HierarchyError(
                f"--hierarchical-cells {cell!r}: a circuit with that name "
                "already exists in the extracted netlist"
            )
    for cell in sorted(by_cell):
        group = by_cell[cell]
        _require_identical(cell, group[0], group[1:])

    # Definitions are built innermost first so a parent can instantiate them.
    built: dict[str, tuple[kdb.Circuit, list[frozenset]]] = {}
    for cell in _innermost_first(by_cell):
        built[cell] = _build_child(netlist, cell, by_cell[cell], built)
    cell_reports = [
        _cell_report(cell, built[cell][0], by_cell[cell]) for cell in sorted(by_cell)
    ]

    plan: list[_InstancePlan] = []
    for cell in sorted(by_cell):
        child, pin_sigs = built[cell]
        top_level = [view for view in by_cell[cell] if view.parent is None]
        plan.extend(
            _InstancePlan(child, f"{cell}_{index}", view, pin_sigs)
            for index, view in enumerate(top_level, start=1)
        )

    _wire_parent(top_circuit, nets, devices, views, plan)
    _check_conservation(pre_class_counts, top_circuit)
    _check_instances(netlist, top_circuit, plan)
    return {
        "cells": cell_reports,
        "top_device_count": sum(1 for _ in top_circuit.each_device()),
        "top_instance_count": sum(1 for _ in top_circuit.each_subcircuit()),
        "circuits": [top_circuit.name, *(entry["circuit"] for entry in cell_reports)],
    }


def _cell_report(
    cell: str, child: kdb.Circuit, group: list[_PlacementView]
) -> dict[str, Any]:
    top_level = sum(1 for view in group if view.parent is None)
    report: dict[str, Any] = {
        "cell": cell,
        "circuit": child.name,
        "placements": len(group),
        "device_count": sum(1 for _ in child.each_device()),
        "pins": [pin.name() for pin in child.each_pin()],
        "instances": [f"X{cell}_{index}" for index in range(1, top_level + 1)],
    }
    # Nested selections (issue #2916): the instances this definition holds,
    # grouped by the definition they instantiate. Absent for a leaf cell, so
    # a non-nested run's JSON is unchanged.
    nested: dict[str, list[str]] = {}
    for sub in child.each_subcircuit():
        nested.setdefault(sub.circuit_ref().name, []).append(f"X{sub.name}")
    if nested:
        report["children"] = [
            {"cell": name, "instances": nested[name]} for name in sorted(nested)
        ]
    return report


def _innermost_first(by_cell: dict[str, list[_PlacementView]]) -> list[str]:
    """Selected cells ordered so every cell follows the cells nested in it."""
    inside: dict[str, set[str]] = {
        cell: {child.cell for view in group for child in view.children}
        for cell, group in by_cell.items()
    }
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(cell: str) -> None:
        if cell in seen:
            return
        seen.add(cell)
        for nested in sorted(inside[cell]):
            visit(nested)
        ordered.append(cell)

    for cell in sorted(by_cell):
        visit(cell)
    return ordered


def _group_by_placement(
    devices: list[kdb.Device], placements: dict[int, tuple[Placement, ...]]
) -> dict[tuple[str, tuple[float, ...]], _PlacementView]:
    """Group the selected devices by ``(cell, placement key)`` and index each
    by its cell-local key (class + position). A device is indexed in every
    placement of its chain (so a view covers its nested placements too) and
    marked *direct* in the innermost one only."""
    views: dict[tuple[str, tuple[float, ...]], _PlacementView] = {}
    for device in devices:
        chain = placements.get(device.id())
        if not chain:
            continue
        for depth, placement in enumerate(chain):
            view = views.setdefault(
                (placement.cell, placement.key),
                _PlacementView(placement.cell, placement.key),
            )
            if depth:
                outer = chain[depth - 1]
                view.parent = views[(outer.cell, outer.key)]
            local_key = (device.device_class().name, *placement.local)
            if local_key in view.devices:
                raise HierarchyError(
                    f"--hierarchical-cells {placement.cell!r}: two recognized "
                    f"devices of class {local_key[0]!r} share cell-local "
                    f"position {placement.local} -- the placement's devices "
                    "cannot be told apart, so a definition would be ambiguous"
                )
            view.devices[local_key] = device
            if depth == len(chain) - 1:
                view.direct_keys.add(local_key)
    for _key, view in sorted(views.items()):
        if view.parent is not None:
            view.parent.children.append(view)
    return views


def _annotate_views(
    top_circuit: kdb.Circuit,
    devices: list[kdb.Device],
    placements: dict[int, tuple[Placement, ...]],
    views: dict[tuple[str, tuple[float, ...]], _PlacementView],
) -> None:
    """Fill each view's net signatures: net -> the cell-local ``(device key,
    terminal)`` set on it, flagged boundary when it also touches a device
    outside the placement's subtree or is a pin of the flat deck."""
    pin_nets = {
        int(net.property(_NET_INDEX_PROPERTY))
        for pin in top_circuit.each_pin()
        for net in [top_circuit.net_for_pin(pin.id())]
        if net is not None
    }
    for view in views.values():
        view.device_keys = sorted(view.devices)
        terms_by_net: dict[int, set[tuple[tuple[str, int, int], int]]] = {}
        for local_key in view.device_keys:
            for terminal_id, net_index in _terminal_nets(view.devices[local_key]):
                terms_by_net.setdefault(net_index, set()).add((local_key, terminal_id))
        outside: set[int] = set()
        for device in devices:
            chain = placements.get(device.id(), ())
            if any((p.cell, p.key) == (view.cell, view.key) for p in chain):
                continue
            outside.update(
                net_index
                for _terminal_id, net_index in _terminal_nets(device)
                if net_index in terms_by_net
            )
        for net_index, terms in terms_by_net.items():
            boundary = net_index in outside or net_index in pin_nets
            view.nets_by_sig[frozenset(terms)] = (net_index, boundary)


def _wire_parent(
    top_circuit: kdb.Circuit,
    nets: list[kdb.Net],
    devices: list[kdb.Device],
    views: dict[tuple[str, tuple[float, ...]], _PlacementView],
    plan: list[_InstancePlan],
) -> None:
    """Instantiate every child in the parent, connect its pins, then drop the
    moved devices and any net nothing references any more."""
    for item in plan:
        sub = top_circuit.create_subcircuit(item.child, item.name)
        for pin, sig in zip(item.child.each_pin(), item.pin_sigs, strict=True):
            sub.connect_pin(pin.id(), nets[item.view.nets_by_sig[sig][0]])

    moved = {d.id() for view in views.values() for d in view.devices.values()}
    for device in devices:
        if device.id() in moved:
            top_circuit.remove_device(device)
    touched = {
        net_index
        for view in views.values()
        for net_index, _boundary in view.nets_by_sig.values()
    }
    for net_index in sorted(touched):
        net = nets[net_index]
        if (
            net.terminal_count() == 0
            and net.pin_count() == 0
            and net.subcircuit_pin_count() == 0
        ):
            top_circuit.remove_net(net)


def _check_conservation(
    pre_class_counts: dict[str, int], top_circuit: kdb.Circuit
) -> None:
    """Every pre-transform device appears exactly once: the top's own devices
    plus, recursively, each definition's devices once per instance path from
    the top (so the check spans every nesting level), per device class."""
    memo: dict[str, dict[str, int]] = {}

    def counts_of(circuit: kdb.Circuit) -> dict[str, int]:
        cached = memo.get(circuit.name)
        if cached is not None:
            return cached
        total = _class_counts(list(circuit.each_device()))
        for sub in circuit.each_subcircuit():
            for name, count in counts_of(sub.circuit_ref()).items():
                total[name] = total.get(name, 0) + count
        memo[circuit.name] = total
        return total

    post_counts = counts_of(top_circuit)
    if post_counts != pre_class_counts:
        raise HierarchyError(
            "--hierarchical-cells conservation check failed -- the "
            f"flat extraction had devices {dict(sorted(pre_class_counts.items()))} "
            f"but the hierarchical netlist accounts for "
            f"{dict(sorted(post_counts.items()))}; refusing to write a "
            "netlist that lost or duplicated a device"
        )


def _check_instances(
    netlist: kdb.Netlist, top_circuit: kdb.Circuit, plan: list[_InstancePlan]
) -> None:
    """Every top-level instance is bound to its definition, and every instance
    in every circuit has every pin connected."""
    subs = {sub.name: sub for sub in top_circuit.each_subcircuit()}
    for item in plan:
        sub = subs.get(item.name)
        if sub is None or sub.circuit_ref().name != item.child.name:
            raise HierarchyError(
                f"--hierarchical-cells instance {item.name!r} is not bound to "
                f"circuit {item.child.name!r}"
            )
    for circuit in netlist.each_circuit():
        for sub in circuit.each_subcircuit():
            for pin in sub.circuit_ref().each_pin():
                if sub.net_for_pin(pin.id()) is None:
                    raise HierarchyError(
                        f"--hierarchical-cells instance {sub.name!r}: pin "
                        f"{pin.name()!r} is not connected to a parent net"
                    )


def _class_counts(devices: list[kdb.Device]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for device in devices:
        name = device.device_class().name
        counts[name] = counts.get(name, 0) + 1
    return counts


def _first_difference(ref: _PlacementView, other: _PlacementView) -> str | None:
    """Why ``other`` is not electrically identical to ``ref``, or ``None``."""
    if other.device_keys != ref.device_keys:
        return (
            f"{len(other.device_keys)} device(s) vs {len(ref.device_keys)} "
            "at different cell-local positions/classes"
        )
    if other.direct_keys != ref.direct_keys or sorted(
        child.cell for child in other.children
    ) != sorted(child.cell for child in ref.children):
        return "different split between its own devices and nested selected cells"
    if set(other.nets_by_sig) != set(ref.nets_by_sig):
        return "different internal connectivity (net partition)"
    for key in ref.device_keys:
        expected = _parameters(ref.devices[key])
        actual = _parameters(other.devices[key])
        for name, value in expected.items():
            if not math.isclose(
                value, actual[name], rel_tol=_PARAMETER_REL_TOL, abs_tol=1e-18
            ):
                return (
                    f"device at {key[1:]} differs in parameter {name} "
                    f"({value} vs {actual[name]})"
                )
    return None


def _require_identical(
    cell: str, ref: _PlacementView, others: list[_PlacementView]
) -> None:
    """All placements of ``cell`` must match ``ref`` device-for-device and
    net-for-net, else one shared definition would be wrong for some of them."""
    for other in others:
        problem = _first_difference(ref, other)
        if problem:
            raise HierarchyError(
                f"--hierarchical-cells {cell!r}: placements are not "
                f"electrically identical ({problem}) -- one shared .SUBCKT "
                "definition would be wrong for some of them"
            )


def _net_label(group: list[_PlacementView], sig: frozenset) -> str:
    """The layout label of the net ``sig`` in the first placement (in
    deterministic order) whose flat net carries one, else ``""``."""
    for view in group:
        label = _label_of(view, view.nets_by_sig[sig][0])
        if label:
            return label
    return ""


def _build_child(
    netlist: kdb.Netlist,
    cell: str,
    group: list[_PlacementView],
    built: dict[str, tuple[kdb.Circuit, list[frozenset]]] | None = None,
) -> tuple[kdb.Circuit, list[frozenset]]:
    """Create the ``.SUBCKT`` for ``cell`` from its first placement. Returns
    the circuit and its pin signatures in pin order. ``built`` holds the
    already-created definitions of the cells nested in ``cell``."""
    import klayout.db as kdb

    ref = group[0]
    boundary_sigs = {
        sig for view in group for sig, (_n, b) in view.nets_by_sig.items() if b
    }
    child = kdb.Circuit()
    child.name = cell
    netlist.add(child)

    used: set[str] = set()
    child_nets: dict[frozenset, kdb.Net] = {}
    for sig in sorted(ref.nets_by_sig, key=min):
        label = _net_label(group, sig)
        if not label and sig in boundary_sigs:
            label = f"p{len(child_nets) + 1}"
        base, suffix = label, 2
        while label and label in used:
            label = f"{base}_{suffix}"
            suffix += 1
        used.add(label)
        child_nets[sig] = child.create_net(label) if label else child.create_net()

    pin_sigs = sorted(boundary_sigs, key=min)
    for sig in pin_sigs:
        pin = child.create_pin(child_nets[sig].name)
        child.connect_pin(pin.id(), child_nets[sig])

    sig_of_terminal = {terminal: sig for sig in child_nets for terminal in sig}
    for key in ref.device_keys:
        if key in ref.direct_keys:
            _copy_device(child, ref.devices[key], key, sig_of_terminal, child_nets)

    # Nested selected placements become instances of their own definitions,
    # wired by the flat net each of their pins resolves to in this placement.
    sig_of_net = {net_index: sig for sig, (net_index, _b) in ref.nets_by_sig.items()}
    instance_counts: dict[str, int] = {}
    for inner in ref.children:
        inner_circuit, inner_pin_sigs = (built or {})[inner.cell]
        index = instance_counts[inner.cell] = instance_counts.get(inner.cell, 0) + 1
        sub = child.create_subcircuit(inner_circuit, f"{inner.cell}_{index}")
        for pin, pin_sig in zip(inner_circuit.each_pin(), inner_pin_sigs, strict=True):
            net_index = inner.nets_by_sig[pin_sig][0]
            sub.connect_pin(pin.id(), child_nets[sig_of_net[net_index]])
    return child, pin_sigs


def _copy_device(
    child: kdb.Circuit,
    source: kdb.Device,
    key: tuple[str, int, int],
    sig_of_terminal: dict[tuple[tuple[str, int, int], int], frozenset],
    child_nets: dict[frozenset, kdb.Net],
) -> None:
    """Copy ``source`` (class, name, parameters, MOS flavour, terminals) into
    ``child``."""
    device_class = source.device_class()
    target = child.create_device(device_class, source.name)
    for pd in device_class.parameter_definitions():
        target.set_parameter(pd.id(), source.parameter(pd.id()))
    flavour = source.property(MOS_FLAVOUR_PROPERTY)
    if flavour is not None:
        target.set_property(MOS_FLAVOUR_PROPERTY, flavour)
    for terminal_def in device_class.terminal_definitions():
        sig = sig_of_terminal.get((key, terminal_def.id()))
        if sig is not None:
            target.connect_terminal(terminal_def.id(), child_nets[sig])


def _label_of(view: _PlacementView, net_index: int) -> str:
    for device in view.devices.values():
        for terminal_def in device.device_class().terminal_definitions():
            net = device.net_for_terminal(terminal_def.id())
            if net is not None and int(net.property(_NET_INDEX_PROPERTY)) == net_index:
                return spice_safe_net_name(net.expanded_name()) if net.name else ""
    return ""
