"""Asymmetric-MOS (LDMOS) S/D orientation pass of the netlist extractor.

Split out of :mod:`klayout_tools.extract` (issues #4570, #4949) to keep that
module under its per-file size ratchet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .pdk_models import MOS_FLAVOUR_PROPERTY

if TYPE_CHECKING:
    import klayout.db as kdb

    from .extract import ExtractionDeck


def orient_asymmetric_mos(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit,
    deck: ExtractionDeck,
    flavour_terminals: list[tuple[kdb.Region, kdb.Region] | None],
    flavour_gates: tuple[list[kdb.Region], list[kdb.Region]],
) -> None:
    """Make each ``drain_marker`` flavour device's ``D`` terminal the net of
    the drain-marked diffusion touching its gate (issue #4570), swapping
    ``S``/``D`` and the matching ``AS``/``AD``/``PS``/``PD`` parameters when
    KLayout's geometric S/D ordering disagrees. The device's position is its
    gate's centre, which selects the gate component and from it the adjacent
    drain polygon whose net is probed."""
    import klayout.db as kdb

    if circuit is None:
        return
    by_flavour = {
        flavour.flavour: (terminals, gates_n + gates_p)
        for flavour, terminals, gates_n, gates_p in zip(
            deck.mos_flavours,
            flavour_terminals,
            flavour_gates[0],
            flavour_gates[1],
            strict=True,
        )
        if terminals is not None
    }
    dbu = l2n.internal_layout().dbu
    for device in circuit.each_device():
        entry = by_flavour.get(device.property(MOS_FLAVOUR_PROPERTY))
        if entry is None:
            continue
        (_source, drain), gates = entry
        centre = device.trans.disp
        probe = kdb.Region(
            kdb.Box(
                round(centre.x / dbu) - 1,
                round(centre.y / dbu) - 1,
                round(centre.x / dbu) + 1,
                round(centre.y / dbu) + 1,
            )
        )
        touching = drain.interacting(gates.interacting(probe))
        polygon = next(iter(touching.each()), None)
        if polygon is None:
            continue
        vertex = next(iter(polygon.each_point_hull()))
        true_drain = l2n.probe_net(drain, kdb.DPoint(vertex.x * dbu, vertex.y * dbu))
        defs = {td.name: td.id() for td in device.device_class().terminal_definitions()}
        s_id, d_id = defs["S"], defs["D"]
        s_net, d_net = device.net_for_terminal(s_id), device.net_for_terminal(d_id)
        if true_drain is None or (
            d_net is not None and d_net.cluster_id == true_drain.cluster_id
        ):
            continue
        # KLayout orders S/D by geometry, so a device whose drain lies on the
        # S side has its ``S`` terminal on a polygon of the ``tD`` layer: it
        # carries no net at all (issue #4949). The true source is then
        # whichever of the two nets is not the drain.
        source_net = s_net if s_net is not None else d_net
        if source_net is not None and source_net.cluster_id == true_drain.cluster_id:
            source_net = d_net
        device.connect_terminal(s_id, source_net)
        device.connect_terminal(d_id, true_drain)
        for a, b in (("AS", "AD"), ("PS", "PD")):
            va, vb = device.parameter(a), device.parameter(b)
            device.set_parameter(a, vb)
            device.set_parameter(b, va)
