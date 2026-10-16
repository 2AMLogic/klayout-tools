"""Tests for the deterministic resistance-graph builder (issue #2980; Stage 3.D5
increment 1 of ``docs/design/extract-fidelity-roadmap.md``).

The fixtures are the Stage 3.D4 matrix rows F1, F2, F4, F5, F6 and F9, drawn at
graph level: illustrative ``Rs = 10 ohm/sq``, strips ``S`` = 20 x 2 um (100 ohm)
and ``H`` = 10 x 2 um (50 ohm), every terminal / rail / via attached at a
strip's *end edge* (a zero-width box). Expected values are the analytic ones
from 3.D4, not captured from the builder. This increment builds the graph only;
the effective-resistance solve is increment 2, so the single-path checks here
sum edge resistances along the one path through the graph.
"""

from __future__ import annotations

import heapq
import json
import random

import klayout.db as kdb
import pytest

from klayout_tools.extract_resistance_graph import (
    LandingInput,
    PortInput,
    ResistanceGraph,
    RoleInput,
    build_resistance_graph,
)

DBU = 0.001
UM = 1000
RS = 10.0
TOL = 1e-9

S_LEN, H_LEN, WIDTH = 20 * UM, 10 * UM, 2 * UM


def _strip(x0: int, y0: int, length: int) -> kdb.Polygon:
    return kdb.Polygon(kdb.Box(x0, y0, x0 + length, y0 + WIDTH))


def _end(x: int, y0: int) -> kdb.Box:
    """A zero-width end-edge box across a strip's width at ``x``."""
    return kdb.Box(x, y0, x, y0 + WIDTH)


def _cuts(length: int, pieces: int, x0: int = 0) -> tuple[int, ...]:
    return tuple(x0 + round(length * i / pieces) for i in range(1, pieces))


def _path_resistance(graph: ResistanceGraph, source: str, sink: str) -> float:
    """Series sum along the (single) path between two nodes: Dijkstra over
    edge resistances, which on a path graph is the one path's sum."""
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.a, []).append((edge.b, edge.resistance_ohm))
        adjacency.setdefault(edge.b, []).append((edge.a, edge.resistance_ohm))
    best = {source: 0.0}
    heap = [(0.0, source)]
    while heap:
        dist, node = heapq.heappop(heap)
        if node == sink:
            return dist
        if dist > best.get(node, float("inf")):
            continue
        for other, r in adjacency.get(node, ()):
            if dist + r < best.get(other, float("inf")):
                best[other] = dist + r
                heapq.heappush(heap, (dist + r, other))
    raise AssertionError(f"{sink} not reachable from {source}")


def _terminal(graph: ResistanceGraph, name: str) -> str:
    return dict(graph.terminals)[name]


def _components(graph: ResistanceGraph) -> int:
    parent = {n.id: n.id for n in graph.nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    count = len(parent)
    for e in graph.edges:
        a, b = find(e.a), find(e.b)
        if a != b:
            parent[a] = b
            count -= 1
    return count


def _f1(pieces: int) -> ResistanceGraph:
    return build_resistance_graph(
        [RoleInput("M1", RS, (_strip(0, 0, S_LEN),), extra_cut_x=_cuts(S_LEN, pieces))],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("B", (("M1", _end(S_LEN, 0)),)),
        ],
        [],
        DBU,
    )


def _f5(pieces: int) -> ResistanceGraph:
    return build_resistance_graph(
        [
            RoleInput(
                "M1", RS, (_strip(0, 0, S_LEN),), extra_cut_x=_cuts(S_LEN, pieces)
            ),
            RoleInput(
                "M2",
                RS,
                (_strip(S_LEN, 0, S_LEN),),
                extra_cut_x=_cuts(S_LEN, pieces, S_LEN),
            ),
        ],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("B", (("M2", _end(2 * S_LEN, 0)),)),
        ],
        [LandingInput("M1", "M2", _end(S_LEN, 0), 0.04, 0.0)],
        DBU,
    )


def _f4() -> ResistanceGraph:
    return build_resistance_graph(
        [
            RoleInput("M1", RS, (_strip(0, 0, S_LEN),)),
            RoleInput("M2", RS, (_strip(0, 0, S_LEN),)),
        ],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("B", (("M1", _end(S_LEN, 0)),)),
        ],
        [
            LandingInput("M1", "M2", _end(0, 0), 0.04, 0.0),
            LandingInput("M1", "M2", _end(S_LEN, 0), 0.04, 0.0),
        ],
        DBU,
    )


@pytest.mark.parametrize("pieces", [1, 2, 7])
def test_f1_single_strip_path_sums_to_100_ohm(pieces):
    graph = _f1(pieces)
    assert graph.status == "ok"
    assert graph.diagnostics == ()
    # n pieces + 2 port nodes; n-1 interior edges + 2 half-piece port edges.
    assert len(graph.nodes) == pieces + 2
    assert len(graph.edges) == pieces + 1
    total = _path_resistance(graph, _terminal(graph, "A"), _terminal(graph, "B"))
    assert total == pytest.approx(100.0, rel=TOL)


@pytest.mark.parametrize("pieces", [1, 2, 7])
def test_f5_two_level_series_path_sums_to_200_ohm(pieces):
    graph = _f5(pieces)
    assert graph.status == "ok"
    assert graph.diagnostics == ()
    # Two strips of n pieces, 2 terminals + 2 landing ports.
    assert len(graph.nodes) == 2 * pieces + 4
    via_edges = [e for e in graph.edges if e.kind == "via"]
    assert len(via_edges) == 1
    assert via_edges[0].resistance_ohm == 0.0
    total = _path_resistance(graph, _terminal(graph, "A"), _terminal(graph, "B"))
    assert total == pytest.approx(200.0, rel=TOL)


def test_f2_parallel_branches_share_two_ideal_rail_nodes():
    n = 4
    ys = [i * 6 * UM for i in range(n)]
    graph = build_resistance_graph(
        [RoleInput("M1", RS, tuple(_strip(0, y, S_LEN) for y in ys))],
        [
            PortInput("RAIL_A", tuple(("M1", _end(0, y)) for y in ys)),
            PortInput("RAIL_B", tuple(("M1", _end(S_LEN, y)) for y in ys)),
        ],
        [],
        DBU,
    )
    assert graph.status == "ok"
    assert len(graph.nodes) == n + 2
    assert len(graph.edges) == 2 * n
    rail_a, rail_b = _terminal(graph, "RAIL_A"), _terminal(graph, "RAIL_B")
    # Every branch is a 100 ohm path rail to rail (parallel solve is
    # increment 2); the rail edges are the two half-piece edges per branch.
    assert _path_resistance(graph, rail_a, rail_b) == pytest.approx(100.0, rel=TOL)
    assert sum(1 for e in graph.edges if rail_a in (e.a, e.b)) == n


def test_f4_two_level_parallel_has_ideal_via_edges_at_both_ends():
    graph = _f4()
    assert graph.status == "ok"
    assert graph.diagnostics == ()
    # 2 pieces, 2 terminals, 4 landing ports.
    assert len(graph.nodes) == 8
    vias = [e for e in graph.edges if e.kind == "via"]
    assert len(vias) == 2
    assert all(e.resistance_ohm == 0.0 for e in vias)
    # 8 edges: 2 terminal + 4 landing attachments + 2 vias.
    assert len(graph.edges) == 8
    assert _components(graph) == 1


def test_f6_overlap_without_landing_creates_no_cross_role_edge():
    graph = build_resistance_graph(
        [
            RoleInput("M1", RS, (_strip(0, 0, S_LEN),)),
            RoleInput("M2", RS, (_strip(0, 0, S_LEN),)),
        ],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("B", (("M1", _end(S_LEN, 0)),)),
        ],
        [],
        DBU,
    )
    assert graph.status == "ok"
    roles = {n.id: n.role for n in graph.nodes}
    for edge in graph.edges:
        assert {roles[edge.a], roles[edge.b]} - {None} == {"M1"}
    # M2 is its own terminal-free component: M1 chain + isolated M2 piece.
    assert _components(graph) == 2
    assert [n.id for n in graph.nodes if n.role == "M2"] == ["M2:0"]
    assert not any("M2:0" in (e.a, e.b) for e in graph.edges)


def test_f9_disconnected_terminal_groups_stay_separate_components():
    graph = build_resistance_graph(
        [RoleInput("M1", RS, (_strip(0, 0, H_LEN), _strip(0, 10 * UM, S_LEN)))],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("B", (("M1", _end(0, 10 * UM)),)),
            PortInput("C", (("M1", _end(S_LEN, 10 * UM)),)),
        ],
        [],
        DBU,
    )
    assert graph.status == "ok"
    assert len(graph.nodes) == 5  # two pieces, three terminals
    assert len(graph.edges) == 3
    assert _components(graph) == 2
    assert _path_resistance(
        graph, _terminal(graph, "B"), _terminal(graph, "C")
    ) == pytest.approx(100.0, rel=TOL)
    # A is only 50 ohm of a dead end: its strip reaches nothing else.
    a_node = _terminal(graph, "A")
    assert not any(
        e.a in (a_node, "M1:0") and e.b in (_terminal(graph, "B"), "M1:1")
        for e in graph.edges
    )


def test_ids_and_ordering_are_rank_based():
    graph = _f5(2)
    pieces = [n.id for n in graph.nodes if n.kind == "piece"]
    assert pieces == ["M1:0", "M1:1", "M2:0", "M2:1"]
    ports = [n.id for n in graph.nodes if n.kind == "port"]
    assert ports == [f"port:{i}" for i in range(4)]
    index = {n.id: i for i, n in enumerate(graph.nodes)}
    keys = [(index[e.a], index[e.b]) for e in graph.edges]
    assert keys == sorted(keys)
    assert all(index[e.a] < index[e.b] for e in graph.edges)


def _dump(graph: ResistanceGraph) -> str:
    return json.dumps(graph.as_dict(), sort_keys=False)


def test_construction_is_byte_identical_across_runs_and_shuffled_input():
    ys = [i * 6 * UM for i in range(4)]
    polygons = [_strip(0, y, S_LEN) for y in ys]
    rails_a = [("M1", _end(0, y)) for y in ys]
    rails_b = [("M1", _end(S_LEN, y)) for y in ys]
    landings = [
        LandingInput("M1", "M2", _end(0, ys[0]), 0.04, 5.0),
        LandingInput("M1", "M2", _end(S_LEN, ys[1]), 0.04, 5.0),
    ]
    upper = [_strip(0, ys[0], S_LEN), _strip(0, ys[1], S_LEN)]

    def build(seed: int | None) -> str:
        rng = random.Random(seed)
        polys, a, b, lands, up = (
            list(polygons),
            list(rails_a),
            list(rails_b),
            list(landings),
            list(upper),
        )
        if seed is not None:
            for seq in (polys, a, b, lands, up):
                rng.shuffle(seq)
        ports = [PortInput("RAIL_A", tuple(a)), PortInput("RAIL_B", tuple(b))]
        if seed is not None:
            rng.shuffle(ports)
        return _dump(
            build_resistance_graph(
                [
                    RoleInput("M1", RS, tuple(polys), extra_cut_x=(7 * UM, 3 * UM)),
                    RoleInput("M2", RS, tuple(up)),
                ],
                ports,
                lands,
                DBU,
            )
        )

    reference = build(None)
    assert build(None) == reference
    for seed in range(6):
        assert build(seed) == reference


def test_port_and_landing_edges_cut_the_polygon_at_their_real_position():
    landing = kdb.Box(5 * UM, 0, 7 * UM, WIDTH)
    graph = build_resistance_graph(
        [
            RoleInput("M1", RS, (_strip(0, 0, S_LEN),)),
            RoleInput("M2", RS, (_strip(0, 0, S_LEN),)),
        ],
        [PortInput("A", (("M1", _end(0, 0)),))],
        # 4 um^2 landing, 1 um^2 cut: 4 cuts at 8 ohm each -> 2 ohm.
        [LandingInput("M1", "M2", landing, 1.0, 8.0)],
        DBU,
    )
    assert graph.status == "ok"
    boxes = {n.id: n.bbox for n in graph.nodes if n.kind == "piece"}
    for role in ("M1", "M2"):
        lefts = sorted(b[0] for i, b in boxes.items() if i.startswith(role))
        rights = sorted(b[2] for i, b in boxes.items() if i.startswith(role))
        assert 5 * UM in lefts and 7 * UM in rights
        assert len(boxes) == 6
    via = [e for e in graph.edges if e.kind == "via"]
    assert [e.resistance_ohm for e in via] == pytest.approx([2.0])
    # The landing overlaps the cut-out piece: contracted by a 0.0 edge.
    zero = [e for e in graph.edges if e.kind == "in_plane" and e.resistance_ohm == 0.0]
    assert len(zero) == 2


def test_ideal_via_edge_is_exactly_zero_and_unknown_via_is_a_diagnostic():
    ideal = _f5(1)
    assert [e.resistance_ohm for e in ideal.edges if e.kind == "via"] == [0.0]

    unknown = build_resistance_graph(
        [
            RoleInput("M1", RS, (_strip(0, 0, S_LEN),)),
            RoleInput("M2", RS, (_strip(0, 0, S_LEN),)),
        ],
        [PortInput("A", (("M1", _end(0, 0)),))],
        [LandingInput("M1", "M2", _end(0, 0), 0.04, None)],
        DBU,
    )
    assert unknown.status == "unsupported"
    assert unknown.nodes == () and unknown.edges == ()
    assert [d.code for d in unknown.diagnostics] == ["via_resistance_unknown"]


def test_missing_sheet_resistance_is_a_diagnostic_not_a_guess():
    graph = build_resistance_graph(
        [RoleInput("M1", None, (_strip(0, 0, S_LEN),))],
        [PortInput("A", (("M1", _end(0, 0)),))],
        [],
        DBU,
    )
    assert graph.status == "unsupported"
    assert [d.code for d in graph.diagnostics] == ["sheet_resistance_unknown"]
    assert graph.diagnostics[0].detail == {"role": "M1"}


def test_non_manhattan_polygon_is_unsupported_geometry():
    triangle = kdb.Polygon(
        [kdb.Point(0, 0), kdb.Point(10 * UM, 0), kdb.Point(0, 10 * UM)]
    )
    graph = build_resistance_graph(
        [RoleInput("M1", RS, (triangle,))],
        [PortInput("A", (("M1", kdb.Box(0, 0, 0, 5 * UM)),))],
        [],
        DBU,
    )
    assert graph.status == "unsupported"
    assert [d.code for d in graph.diagnostics] == ["unsupported_geometry"]
    assert graph.nodes == ()


def test_oversized_graphs_report_graph_too_large_with_counts():
    def build(**kwargs):
        return build_resistance_graph(
            [RoleInput("M1", RS, (_strip(0, 0, S_LEN),), extra_cut_x=_cuts(S_LEN, 7))],
            [
                PortInput("A", (("M1", _end(0, 0)),)),
                PortInput("B", (("M1", _end(S_LEN, 0)),)),
            ],
            [],
            DBU,
            **kwargs,
        )

    assert build().status == "ok"
    for kwargs, key in (
        ({"max_cells": 3}, "role"),
        ({"max_nodes": 8}, "nodes"),
        ({"max_edges": 7}, "edges"),
        ({"max_terminals": 1}, "terminals"),
    ):
        graph = build(**kwargs)
        assert graph.status == "unsupported", kwargs
        assert [d.code for d in graph.diagnostics] == ["graph_too_large"], kwargs
        assert key in graph.diagnostics[0].detail, kwargs
        assert graph.nodes == () and graph.edges == ()
    counts = build(max_nodes=8).diagnostics[0].detail
    assert counts["nodes"] == 9 and counts["max_nodes"] == 8


def test_port_touching_no_conductor_is_informational():
    graph = build_resistance_graph(
        [RoleInput("M1", RS, (_strip(0, 0, S_LEN),))],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("FAR", (("M1", kdb.Box(50 * UM, 0, 50 * UM, WIDTH)),)),
        ],
        [],
        DBU,
    )
    assert graph.status == "ok"
    assert [d.code for d in graph.diagnostics] == ["unattached_port"]
    assert graph.diagnostics[0].detail["port"] == "FAR"


def test_unknown_role_and_bad_cut_area_are_caller_errors():
    roles = [RoleInput("M1", RS, (_strip(0, 0, S_LEN),))]
    with pytest.raises(ValueError, match="unknown role"):
        build_resistance_graph(roles, [PortInput("A", (("M9", _end(0, 0)),))], [], DBU)
    with pytest.raises(ValueError, match="cut_area_um2"):
        build_resistance_graph(
            roles, [], [LandingInput("M1", "M1", _end(0, 0), 0.0, 1.0)], DBU
        )
