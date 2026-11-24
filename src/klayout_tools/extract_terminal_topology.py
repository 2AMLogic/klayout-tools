"""The per-net terminal-topology models shared by the star, the
``--distributed-rc`` ladder and ``klt pex --resistance-network``'s reduced
terminal network (issue #6899).

``_terminal_star_positions_um``/``_terminal_star_weights``/
``_distributed_rc_order``/``_distributed_rc_segments`` moved verbatim from
``extract_parasitics.py`` (issues #592/#977; issue #6899 pays for its
emission growth with the move -- behavior unchanged, and
``extract_parasitics`` re-imports them for its own and the test suite's
existing call sites). ``emit_reduced_network`` is #6899's emission adapter:
it instantiates one net's Kron-reduced terminal network
(:mod:`klayout_tools.extract_resistance_network`) inside the circuit
``_inject_parasitics`` hands it. The extract-side helpers it needs are
imported lazily inside the functions -- ``extract_parasitics`` imports this
module at its top level, so a module-level import back would be circular.
"""

from __future__ import annotations

import math
from typing import Any


def _terminal_star_positions_um(terminal_refs: list[Any]) -> list[tuple[float, float]]:
    """The approximate ``(x_um, y_um)`` location of each of ``terminal_refs``,
    read from its owning device's ``Device.trans`` (already reported in real
    micrometres -- unlike most of this module, no ``dbu`` scaling is needed).

    This is a **coarse** proxy for a terminal's true physical location
    (issue #592): KLayout's connectivity extraction records one placement
    transform per *device* (typically the center of its recognition shape),
    not a distinct position per terminal, so every terminal on the same
    device instance shares that device's single location. Good enough to
    rank a net's terminals by their approximate spread for the star-topology
    resistance split; not a substitute for true per-segment routing
    measurement (out of scope -- issue #592's deferred Option 2)."""
    positions: list[tuple[float, float]] = []
    for ref in terminal_refs:
        disp = ref.device().trans.disp
        positions.append((disp.x, disp.y))
    return positions


def _terminal_star_weights(positions: list[tuple[float, float]]) -> list[float]:
    """Normalized (sum to ``1.0``) per-terminal resistance-split weights for
    the star topology: proportional to each position's Euclidean distance
    from the centroid of ``positions``, so a terminal placed farther from a
    net's other terminals is assigned more of that net's total resistance --
    a coarse, position-aware stand-in for "farther terminals see more
    interconnect" without a full per-segment routing model (issue #592).

    Degenerates to an equal ``1/N`` split when every position coincides
    (including ``N == 1``, where the lone terminal necessarily sits at the
    "centroid") -- this is what makes a single-terminal net's star reduce to
    exactly the pre-#592 Gamma-shunt's one lumped resistor: same total value,
    not a smaller or larger one."""
    n = len(positions)
    if n == 0:
        return []
    cx = sum(p[0] for p in positions) / n
    cy = sum(p[1] for p in positions) / n
    distances = [math.hypot(x - cx, y - cy) for x, y in positions]
    total = sum(distances)
    if total <= 0.0:
        return [1.0 / n] * n
    return [d / total for d in distances]


def _distributed_rc_order(positions: list[tuple[float, float]]) -> list[int]:
    """An index permutation of ``positions`` approximating a physical tap
    order along a net's dominant spread axis (``--distributed-rc``, issue
    #977, Epic #709 Phase 2b): a ladder needs a linear chain, not an
    unordered set, so terminals must be placed in *some* sequence before
    :func:`_distributed_rc_segments` can derive adjacent-pair segment R/C.

    Finds the pair of positions with the greatest pairwise Euclidean
    distance (the same coarse "how spread out is this net" signal
    :func:`_terminal_star_weights` already leans on), projects every
    position onto the axis between that pair, and sorts by the projection
    -- a deterministic, position-only proxy for "which end is which" that
    needs no routing/skeleton geometry (:func:`_terminal_star_positions_um`'s
    own docstring: terminal position is a device-placement proxy, not true
    routed geometry).

    Degenerates to the input order (``[0, 1, ..., n - 1]``) for ``n <= 2``
    (nothing to order between two points) and whenever every position
    coincides (the projection axis is undefined) -- harmless, since every
    segment length is then ``0.0`` too and :func:`_distributed_rc_segments`'s
    equal-split fallback takes over regardless of order.
    """
    n = len(positions)
    if n <= 2:
        return list(range(n))

    best_pair = (0, 1)
    best_dist = -1.0
    for i in range(n):
        xi, yi = positions[i]
        for j in range(i + 1, n):
            xj, yj = positions[j]
            dist = math.hypot(xj - xi, yj - yi)
            if dist > best_dist:
                best_dist = dist
                best_pair = (i, j)
    if best_dist <= 0.0:
        return list(range(n))

    ai, aj = best_pair
    ax, ay = positions[ai]
    bx, by = positions[aj]
    ux, uy = bx - ax, by - ay
    norm = math.hypot(ux, uy)
    ux, uy = ux / norm, uy / norm
    projections = [
        ((x - ax) * ux + (y - ay) * uy, idx) for idx, (x, y) in enumerate(positions)
    ]
    projections.sort(key=lambda item: (item[0], item[1]))
    return [idx for _, idx in projections]


def _distributed_rc_segments(
    positions: list[tuple[float, float]],
    r_total_ohm: float,
    c_total_ff: float,
) -> tuple[list[int], list[float], list[float]]:
    """Break one net's total lumped resistance/capacitance into a chain
    ("ladder") along ``positions``' approximate physical order, for
    ``--distributed-rc`` (issue #977, Epic #709 Phase 2b) -- the multi-
    segment alternative to :func:`_inject_parasitics`'s star topology.

    Returns ``(order, segment_r_ohm, node_c_ff)``, all keyed to
    ``positions``' own indices:

    - ``order``: :func:`_distributed_rc_order`'s index permutation (length
      ``N``, one entry per terminal) -- the ladder's node sequence.
    - ``segment_r_ohm``: ``N - 1`` per-segment series resistances, one
      between each adjacent pair in ``order``. Each segment's share of
      ``r_total_ohm`` is proportional to that pair's Euclidean distance
      (its approximate physical length), so the segments always sum back
      to exactly ``r_total_ohm`` -- the same "legs sum back to the net's
      total" invariant :func:`_terminal_star_weights` already guarantees
      for the star topology.
    - ``node_c_ff``: ``N`` per-terminal ground capacitances, in ``order``'s
      sequence. Each terminal's share of ``c_total_ff`` is the *average* of
      its one or two adjacent segments' length share (an interior node
      touches two segments, an end node touches one) -- the standard "half
      the capacitance of each adjoining segment" lumped-element
      discretization of a distributed RC line. Node capacitances also sum
      back to exactly ``c_total_ff``.

    Degenerates to an equal split (``r_total_ohm / (N - 1)`` per segment,
    ``c_total_ff / N`` per node) when every position coincides -- the same
    "no spatial signal available" fallback :func:`_terminal_star_weights`
    already uses, and still conserves both totals.

    ``positions`` must have at least 2 entries -- a distributed ladder needs
    at least one segment; a net with 0 or 1 terminal has nothing to chain
    and stays on the star/Gamma-shunt path in :func:`_inject_parasitics`.
    """
    order = _distributed_rc_order(positions)
    n = len(order)
    ordered = [positions[i] for i in order]
    seg_lengths = [
        math.hypot(ordered[i + 1][0] - ordered[i][0], ordered[i + 1][1] - ordered[i][1])
        for i in range(n - 1)
    ]
    total_length = sum(seg_lengths)

    if total_length <= 0.0:
        segment_r_ohm = [r_total_ohm / (n - 1)] * (n - 1)
        node_c_ff = [c_total_ff / n] * n
        return order, segment_r_ohm, node_c_ff

    segment_r_ohm = [r_total_ohm * (length / total_length) for length in seg_lengths]
    node_c_ff = []
    for i in range(n):
        adjacent_lengths = seg_lengths[max(i - 1, 0) : min(i + 1, n - 1)]
        share = sum(adjacent_lengths) / (2.0 * total_length)
        node_c_ff.append(c_total_ff * share)
    return order, segment_r_ohm, node_c_ff


def emit_reduced_network(
    circuit: Any,
    res_class: Any,
    cap_class: Any,
    ground: Any,
    ground_uid: int,
    integ: Any,
    entry: dict[str, Any],
    net: Any,
    net_uid: int,
    terminal_refs: list[Any],
    graph_report: dict[str, Any],
    network: Any,
    mapping: Any,
    existing_names: set[str],
    instance_name: str,
    origin_id: int,
    record_slice_topology: bool,
) -> tuple[Any, str, list[int], list[dict[str, Any]], int, float]:
    """Emit one net's Kron-reduced terminal network (issue #6899; the Stage
    3 netlist step of ``docs/design/extract-fidelity-roadmap.md`` 3.D3):
    one resistor per surviving terminal pair between per-terminal leg nets
    -- no hub, no star legs, no series-summed stub resistance in the
    current path.

    The *middle* device terminal (in ``net.each_terminal()`` order -- the
    same "one position reuses ``net``" convention the distributed ladder
    uses) keeps the original net object so a promoted pin stays attached,
    and doubles as this net's coupling/ground attachment point. The net's
    ground capacitance splits equally across the N terminal nodes (the
    documented first-increment rule; per-node capacitance from the graph
    itself is the roadmap's open question). Returns ``(hub, hub_name,
    leg_uids, terminal_reports, device_count, emitted_r_ohm)``.
    """
    from .extract import _MIN_PARASITIC_R_OHM
    from .extract_parasitics import (
        _emitted_capacitance_ff,
        _tag_slice_origin,
        _unique_net_name,
        spice_safe_net_name,
    )

    terminal_count = len(terminal_refs)
    mid_index = terminal_count // 2
    legs = [net] * terminal_count
    leg_names = [entry["net"]] * terminal_count
    leg_uids = [net_uid] * terminal_count
    leg_of_graph = [0] * terminal_count
    node_c_farad = entry["capacitance_ff"] * 1e-15 / terminal_count
    device_count = 0
    terminal_reports: list[dict[str, Any]] = []
    for i, term_ref in enumerate(terminal_refs):
        device = term_ref.device()
        terminal_def = term_ref.terminal_def()
        leg_of_graph[mapping.graph_indices[i]] = i
        if i != mid_index:
            leg_name = _unique_net_name(entry["net"], existing_names, suffix=f"__t{i}")
            existing_names.add(leg_name)
            legs[i] = circuit.create_net(leg_name)
            leg_names[i] = leg_name
            _tag_slice_origin(legs[i], origin_id, record_slice_topology)
            device.disconnect_terminal(terminal_def.id())
            device.connect_terminal(terminal_def.id(), legs[i])
            leg_uids[i] = integ.add_node(spice_safe_net_name(leg_name), origin_id)
        node_cap = circuit.create_device(cap_class, f"{instance_name}_n{i}")
        node_cap.connect_terminal("A", legs[i])
        node_cap.connect_terminal("B", ground)
        node_cap.set_parameter("C", node_c_farad)
        device_count += 1
        integ.add_ground_capacitor(
            f"{instance_name}_n{i}",
            leg_uids[i],
            ground_uid,
            _emitted_capacitance_ff(node_cap),
            origin_id,
        )
        terminal_reports.append(
            {
                "device": device.expanded_name(),
                "terminal": terminal_def.name,
                # Report-boundary escape (issue #1162), same rationale as
                # the star/ladder paths' own entries.
                "leg_net": spice_safe_net_name(leg_names[i]),
                # The geometry-derived graph terminal this leg was matched
                # to (issue #6899's terminal mapping).
                "graph_terminal": graph_report["terminals"][mapping.graph_indices[i]],
            }
        )
    emitted = 0.0
    for k, resistor in enumerate(network.resistors):
        resistor_ohm = max(resistor.resistance_ohm, _MIN_PARASITIC_R_OHM)
        r_dev = circuit.create_device(res_class, f"{instance_name}_g{k}")
        r_dev.connect_terminal("A", legs[leg_of_graph[resistor.terminal_a]])
        r_dev.connect_terminal("B", legs[leg_of_graph[resistor.terminal_b]])
        r_dev.set_parameter("R", resistor_ohm)
        device_count += 1
        emitted += resistor_ohm
        integ.add_resistor(
            f"{instance_name}_g{k}",
            leg_uids[leg_of_graph[resistor.terminal_a]],
            leg_uids[leg_of_graph[resistor.terminal_b]],
            resistor_ohm,
            origin_id,
        )
    return (
        legs[mid_index],
        leg_names[mid_index],
        leg_uids,
        terminal_reports,
        device_count,
        emitted,
    )
