"""Index promoted circuit pins by KLayout cluster for net reports and pin renaming."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb


def _pin_index_by_net_id(circuit: kdb.Circuit) -> dict[int, int]:
    """Map each promoted pin's underlying net's ``cluster_id`` to its 0-based
    position in ``circuit.each_pin()`` order -- issue #1540.

    KLayout's ``NetlistSpiceWriter`` emits ports in this order. Even when names
    collide, a caller can match the original port position to a ``nets[]`` row
    via ``net_id`` without an LVS run. Explicit ``--pins`` can reorder the final
    top-level ``.SUBCKT`` header after writing. Manually created nets with
    cluster ID zero collapse onto one key (the last pin wins).
    """
    result: dict[int, int] = {}
    for index, pin in enumerate(circuit.each_pin()):
        net = circuit.net_for_pin(pin.id())
        if net is not None:
            result[net.cluster_id] = index
    return result


def _promoted_pin_nets_by_cluster(circuit: Any) -> dict[int, tuple[Any, list[int]]]:
    """Every promoted pin's net of ``circuit`` exactly once, keyed by
    ``Net.cluster_id`` and in ``each_pin()`` order (deterministic for a given
    netlist): two pins can resolve to one net object, and the rename pass
    (issue #2854) must process -- and rename -- that net once. Values are
    ``(net, [pin_id, ...])``.

    A pin with no net, or whose net carries no name, is skipped: there is
    nothing to match a declared name against.
    """
    pin_nets: dict[int, tuple[Any, list[int]]] = {}
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is None or not net.expanded_name():
            continue
        entry = pin_nets.setdefault(net.cluster_id, (net, []))
        entry[1].append(pin.id())
    return pin_nets
