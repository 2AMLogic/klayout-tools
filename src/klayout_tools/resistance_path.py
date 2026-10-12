"""Shortest **series** path resistance over a resistor graph (issue #2765).

A pure, bounded query: given a resistor multigraph and two node IDs, return
the minimum *sum of series resistor values* over any path between them
(Dijkstra over non-negative edge weights), together with the ordered node
and resistor IDs of the path that achieves it.

What this is **not**:

- not the *effective* two-port resistance between the nodes -- that needs a
  Laplacian (Kirchhoff) solve over every parallel path at once; two parallel
  2 ohm and 3 ohm resistors are reported here as ``2.0`` ohm (the cheaper
  single path), not ``1.2`` ohm;
- not an IR-drop solve, and not a physical-route resistance recovered from
  layout geometry -- the result is exactly as good as the graph it is given.

Two layers:

- :func:`shortest_series_resistance` -- the generic helper over caller-
  supplied ``nodes``/``resistors`` sequences, so later adapters (e.g. a
  ``klt power`` resistive network) can reuse it unchanged.
- :func:`query_parasitic_net` -- the adapter over ``klt extract
  --parasitics --distributed-rc``'s own ``parasitics.nets[]`` report
  entries: a distributed net's ``terminals[].leg_net`` values are its nodes
  and its ``segments[]`` its resistors.

Both are pure (no KLayout import, no I/O). The query contract -- statuses,
payload, limits, validation, tie-breaking -- is documented in
``docs/cli/extract.md``'s "Point-to-point series resistance
(``--resistance-path``)" section.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The quantity every result reports, stated machine-readably so a consumer
#: cannot mistake it for an effective (two-port) resistance.
QUANTITY = "shortest_series_path_resistance"

#: Default node cap: a graph with more nodes than this is refused with
#: ``limit_exceeded`` before any adjacency is built. A ``--distributed-rc``
#: ladder has one node per device terminal of the net.
DEFAULT_MAX_NODES = 10_000

#: Default resistor cap: a graph with more resistors than this is refused
#: with ``limit_exceeded`` before any adjacency is built. A
#: ``--distributed-rc`` ladder over ``N`` terminals has ``N - 1`` resistors.
DEFAULT_MAX_RESISTORS = 10_000

STATUS_OK = "ok"
STATUS_LIMIT_EXCEEDED = "limit_exceeded"
STATUS_NODE_NOT_FOUND = "node_not_found"
STATUS_NO_PATH = "no_path"

#: Every status value a result can carry.
STATUSES = (STATUS_OK, STATUS_LIMIT_EXCEEDED, STATUS_NODE_NOT_FOUND, STATUS_NO_PATH)


class ResistanceGraphError(ValueError):
    """Malformed graph input: a duplicate node or resistor ID, a resistor
    endpoint naming no node, or a negative/non-finite/non-numeric resistance.

    Raised before any search, so a malformed graph never yields an ``ok`` or
    ``no_path`` answer computed over silently-dropped edges.
    """


@dataclass(frozen=True)
class GraphResistor:
    """One two-terminal resistor of the graph. ``id`` must be unique within
    the graph; ``node_a``/``node_b`` must name nodes of the graph (equal
    values -- a self-loop -- are accepted and can never lie on a shortest
    path)."""

    id: str
    node_a: str
    node_b: str
    resistance_ohm: float


def validate_limits(max_nodes: Any, max_resistors: Any) -> None:
    """Raise :class:`ValueError` unless both caps are positive integers
    (``bool`` is rejected even though it is an ``int`` subclass)."""
    for label, value in (("max_nodes", max_nodes), ("max_resistors", max_resistors)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label} must be a positive integer (got {value!r})")


def _result(
    status: str,
    reason: str | None,
    node_count: int | None,
    resistor_count: int | None,
    resistance_ohm: float | None = None,
    path_nodes: list[str] | None = None,
    path_resistors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "resistance_ohm": resistance_ohm,
        "path_nodes": path_nodes or [],
        "path_resistors": path_resistors or [],
        "node_count": node_count,
        "resistor_count": resistor_count,
    }


def _validate_graph(
    nodes: Sequence[str], resistors: Sequence[GraphResistor]
) -> dict[str, int]:
    """Return ``{node_id: input_position}``; raise
    :class:`ResistanceGraphError` on any malformed entry."""
    index: dict[str, int] = {}
    for position, node in enumerate(nodes):
        if node in index:
            raise ResistanceGraphError(f"duplicate node id {node!r}")
        index[node] = position
    seen_ids: set[str] = set()
    for resistor in resistors:
        if resistor.id in seen_ids:
            raise ResistanceGraphError(f"duplicate resistor id {resistor.id!r}")
        seen_ids.add(resistor.id)
        for endpoint in (resistor.node_a, resistor.node_b):
            if endpoint not in index:
                raise ResistanceGraphError(
                    f"resistor {resistor.id!r} endpoint {endpoint!r} names no node "
                    "of the graph"
                )
        value = resistor.resistance_ohm
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ResistanceGraphError(
                f"resistor {resistor.id!r} resistance_ohm must be a finite, "
                f"non-negative number (got {value!r})"
            )
    return index


def shortest_series_resistance(
    nodes: Sequence[str],
    resistors: Sequence[GraphResistor],
    source: str,
    target: str,
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_resistors: int = DEFAULT_MAX_RESISTORS,
) -> dict[str, Any]:
    """Minimum series resistance between ``source`` and ``target``.

    Checks run in this order, each before the next does any work:

    1. ``max_nodes``/``max_resistors`` must be positive integers
       (:class:`ValueError` otherwise).
    2. ``len(nodes) > max_nodes`` or ``len(resistors) > max_resistors`` ->
       ``limit_exceeded``; nothing is validated or allocated. A graph
       exactly at a cap is searched.
    3. The graph is validated (:class:`ResistanceGraphError`).
    4. An endpoint absent from ``nodes`` -> ``node_not_found`` (this holds
       even when ``source == target``).
    5. ``source == target`` -> ``ok`` with ``0.0`` and a one-node path.
    6. Dijkstra; an unreachable ``target`` -> ``no_path``.

    The graph is an undirected multigraph: parallel resistors stay separate
    edges (never combined into an effective value), zero-ohm edges and
    zero-ohm cycles are fine (a node is settled once), and self-loops are
    ignored by the search.

    **Tie-breaking.** Nodes are settled in ``(distance, input position)``
    order and each node's resistors are scanned in input order; a candidate
    path replaces a node's current best only when strictly cheaper. Among
    equal-cost paths the result is therefore fixed for a given input order
    (same input, same path, every run) -- reordering ``nodes``/``resistors``
    may select a different, equally cheap path, but never a different
    ``resistance_ohm``.

    Returns ``{"status", "reason", "resistance_ohm", "path_nodes",
    "path_resistors", "node_count", "resistor_count"}``. Only ``ok`` carries
    a resistance (a finite float, the plain float sum of the path's values)
    and non-empty paths; every other status carries ``None`` and ``[]``.
    """
    validate_limits(max_nodes, max_resistors)
    node_count = len(nodes)
    resistor_count = len(resistors)
    if node_count > max_nodes or resistor_count > max_resistors:
        return _result(
            STATUS_LIMIT_EXCEEDED,
            f"graph has {node_count} node(s) and {resistor_count} resistor(s); "
            f"limits are max_nodes={max_nodes}, max_resistors={max_resistors}",
            node_count,
            resistor_count,
        )

    index = _validate_graph(nodes, resistors)
    missing = [name for name in (source, target) if name not in index]
    if missing:
        return _result(
            STATUS_NODE_NOT_FOUND,
            "endpoint(s) "
            + ", ".join(repr(name) for name in dict.fromkeys(missing))
            + " name no node of the graph",
            node_count,
            resistor_count,
        )
    if source == target:
        return _result(STATUS_OK, None, node_count, resistor_count, 0.0, [source], [])

    start = index[source]
    goal = index[target]
    adjacency = _build_adjacency(node_count, index, resistors)
    distance, previous = _dijkstra(adjacency, start, goal)
    if previous[goal] is None:
        return _result(
            STATUS_NO_PATH,
            f"no resistor path connects {source!r} and {target!r}",
            node_count,
            resistor_count,
        )

    path_nodes = [nodes[goal]]
    path_resistors: list[str] = []
    cursor = goal
    while cursor != start:
        step = previous[cursor]
        assert step is not None  # every reached non-start node has one
        cursor, resistor_position = step
        path_resistors.append(resistors[resistor_position].id)
        path_nodes.append(nodes[cursor])
    path_nodes.reverse()
    path_resistors.reverse()
    return _result(
        STATUS_OK,
        None,
        node_count,
        resistor_count,
        distance,
        path_nodes,
        path_resistors,
    )


def _build_adjacency(
    node_count: int, index: Mapping[str, int], resistors: Sequence[GraphResistor]
) -> list[list[tuple[int, int, float]]]:
    """Per-node ``(neighbour position, resistor position, resistance)``
    lists, in resistor input order; self-loops are left out."""
    adjacency: list[list[tuple[int, int, float]]] = [[] for _ in range(node_count)]
    for position, resistor in enumerate(resistors):
        a = index[resistor.node_a]
        b = index[resistor.node_b]
        if a == b:
            continue
        r_ohm = float(resistor.resistance_ohm)
        adjacency[a].append((b, position, r_ohm))
        adjacency[b].append((a, position, r_ohm))
    return adjacency


def _dijkstra(
    adjacency: list[list[tuple[int, int, float]]], start: int, goal: int
) -> tuple[float, list[tuple[int, int] | None]]:
    """Return ``(distance to goal, previous)`` where ``previous[n]`` is the
    ``(previous node, resistor position)`` step of the best path found to
    ``n`` (``None`` for ``start`` and for every unreached node). Stops once
    ``goal`` is settled; ``start != goal``."""
    node_count = len(adjacency)
    distance: list[float] = [math.inf] * node_count
    previous: list[tuple[int, int] | None] = [None] * node_count
    settled = [False] * node_count
    distance[start] = 0.0
    heap: list[tuple[float, int]] = [(0.0, start)]
    while heap:
        dist, node = heapq.heappop(heap)
        if settled[node]:
            continue
        settled[node] = True
        if node == goal:
            break
        for neighbour, resistor_position, r_ohm in adjacency[node]:
            candidate = dist + r_ohm
            if not settled[neighbour] and candidate < distance[neighbour]:
                distance[neighbour] = candidate
                previous[neighbour] = (node, resistor_position)
                heapq.heappush(heap, (candidate, neighbour))
    return distance[goal], previous


#: Decimal places ``klt extract`` rounds ``segments[].resistance_ohm`` to --
#: the ladder adapter sums those serialized values, so its result is rounded
#: to the same precision (the sum of 4-decimal values is itself a 4-decimal
#: value; rounding only strips float noise).
_REPORT_R_DECIMALS = 4


def segment_resistor_id(segment_index: int) -> str:
    """The query-local ID of a distributed net's ``segments[segment_index]``
    -- ``"seg<i>"``, scoped to that one net's report entry (it is the
    suffix of the written netlist's own ``R<net>_seg<i>`` instance name, but
    consumers should treat it as an index into ``segments[]``)."""
    return f"seg{segment_index}"


def query_parasitic_net(
    report_nets: Sequence[Mapping[str, Any]],
    net_id: int,
    source: str,
    target: str,
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_resistors: int = DEFAULT_MAX_RESISTORS,
) -> dict[str, Any]:
    """Run :func:`shortest_series_resistance` over one ``klt extract
    --parasitics --distributed-rc`` net's emitted ladder.

    ``report_nets`` is ``parasitics.nets[]``; the net is selected by its
    ``net_id`` only (never by display name, which can collide across
    distinct net islands). Its graph is exactly that entry's own
    ``terminals[].leg_net`` nodes and ``segments[]`` resistors -- no other
    net, capacitor, device or substrate tie can be traversed. A ``net_id``
    matching no entry, or one whose ``rc_model`` is not ``"distributed"``,
    is ``node_not_found`` with a ``reason``.

    Resistances are the report's own serialized ``segments[].resistance_ohm``
    values (rounded to 4 decimals at emission; the written netlist carries
    the unrounded value, so the two differ by at most ``5e-5`` ohm per
    segment). The returned ``resistance_ohm`` is rounded to the same 4
    decimals.

    Returns the helper's payload plus the query echo and model identity:
    ``net_id``, ``net``, ``rc_model``, ``from``, ``to``, ``quantity``,
    ``max_nodes``, ``max_resistors``. Raises :class:`ValueError` for
    invalid limits and :class:`ResistanceGraphError` for a malformed entry.
    """
    validate_limits(max_nodes, max_resistors)
    echo: dict[str, Any] = {
        "net_id": net_id,
        "net": None,
        "rc_model": None,
        "from": source,
        "to": target,
        "quantity": QUANTITY,
    }
    limits = {"max_nodes": max_nodes, "max_resistors": max_resistors}
    entry = next((e for e in report_nets if e.get("net_id") == net_id), None)
    if entry is None:
        result = _result(
            STATUS_NODE_NOT_FOUND,
            f"no parasitics.nets[] entry has net_id {net_id}",
            None,
            None,
        )
        return {**echo, **result, **limits}

    echo["net"] = entry.get("net")
    echo["rc_model"] = entry.get("rc_model")
    if entry.get("rc_model") != "distributed":
        result = _result(
            STATUS_NODE_NOT_FOUND,
            f"net_id {net_id} ({entry.get('net')!r}) has rc_model "
            f"{entry.get('rc_model')!r}; resistance paths are defined only over "
            "a --distributed-rc ladder (name the net with --critical-net)",
            None,
            None,
        )
        return {**echo, **result, **limits}

    terminals = entry.get("terminals") or []
    segments = entry.get("segments") or []
    nodes = [terminal["leg_net"] for terminal in terminals]
    resistors = [
        GraphResistor(
            segment_resistor_id(i),
            segment["net_a"],
            segment["net_b"],
            segment["resistance_ohm"],
        )
        for i, segment in enumerate(segments)
    ]
    result = shortest_series_resistance(
        nodes,
        resistors,
        source,
        target,
        max_nodes=max_nodes,
        max_resistors=max_resistors,
    )
    if result["resistance_ohm"] is not None:
        result["resistance_ohm"] = round(result["resistance_ohm"], _REPORT_R_DECIMALS)
    return {**echo, **result, **limits}
