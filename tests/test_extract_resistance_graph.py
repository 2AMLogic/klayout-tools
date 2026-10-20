"""Tests for the deterministic resistance-graph builder (issue #2980; Stage 3.D5
increment 1 of ``docs/design/extract-fidelity-roadmap.md``).

The fixtures are the Stage 3.D4 matrix rows F1, F2, F4, F5, F6 and F9, drawn at
graph level: illustrative ``Rs = 10 ohm/sq``, strips ``S`` = 20 x 2 um (100 ohm)
and ``H`` = 10 x 2 um (50 ohm), every terminal / rail / via attached at a
strip's *end edge* (a zero-width box). Expected values are the analytic ones
from 3.D4, not captured from the builder. This increment builds the graph only;
the effective-resistance solve is increment 2, so the single-path checks here
sum edge resistances along the one path through the graph.

The solve tests (issue #2981, increment 2) run the same fixtures through the
``solve_ir_drop`` adapter against the 3.D4 analytic results; the F2v / tied-F8
variants and the monotonicity relations are asserted exactly as 3.D4 scopes
them.
"""

from __future__ import annotations

import heapq
import json
import random

import klayout.db as kdb
import pytest

from klayout_tools import extract_resistance_graph as erg
from klayout_tools.extract_resistance_graph import (
    GraphEdge,
    GraphNode,
    LandingInput,
    PortInput,
    ResistanceGraph,
    RoleInput,
    build_resistance_graph,
    effective_resistance,
    terminal_resistance_matrix,
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


def _f4(r_cut: float = 0.0) -> ResistanceGraph:
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
            LandingInput("M1", "M2", _end(0, 0), 0.04, r_cut),
            LandingInput("M1", "M2", _end(S_LEN, 0), 0.04, r_cut),
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
    # 2 pieces, 2 terminals that double as the M1 landing ports, 2 M2 ports.
    assert len(graph.nodes) == 6
    vias = [e for e in graph.edges if e.kind == "via"]
    assert len(vias) == 2
    assert all(e.resistance_ohm == 0.0 for e in vias)
    # 6 edges: 2 terminal + 2 M2 landing attachments + 2 vias.
    assert len(graph.edges) == 6
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


# -- Effective resistance: adapter over solve_ir_drop (issue #2981) -----------

TOL6 = 1e-6
Y_STEP = 4 * UM


def _two_terminal(graph: ResistanceGraph, a: str, b: str) -> float:
    matrix = terminal_resistance_matrix(graph)
    assert matrix.status == "ok"
    value = matrix.pair_resistance_ohm[matrix.terminals.index(a)][
        matrix.terminals.index(b)
    ]
    assert value is not None
    return value


def _rails(lengths: list[int]) -> ResistanceGraph:
    """N separated level-1 strips between ideal rails RAIL_A / RAIL_B (F2, F7)."""
    ys = [i * Y_STEP for i in range(len(lengths))]
    return build_resistance_graph(
        [
            RoleInput(
                "M1",
                RS,
                tuple(_strip(0, y, n) for y, n in zip(ys, lengths, strict=True)),
            )
        ],
        [
            PortInput("RAIL_A", tuple(("M1", _end(0, y)) for y in ys)),
            PortInput(
                "RAIL_B",
                tuple(("M1", _end(n, y)) for y, n in zip(ys, lengths, strict=True)),
            ),
        ],
        [],
        DBU,
    )


def _star_arms() -> ResistanceGraph:
    """F8: arms a=S, b=H, c=30um joined at ideal HUB; terminals at outer ends."""
    lengths = {"A": S_LEN, "B": H_LEN, "C": 30 * UM}
    ys = {name: i * Y_STEP for i, name in enumerate(lengths)}
    return build_resistance_graph(
        [RoleInput("M1", RS, tuple(_strip(0, ys[k], n) for k, n in lengths.items()))],
        [
            *(PortInput(k, (("M1", _end(0, ys[k])),)) for k in lengths),
            PortInput(
                "HUB",
                tuple(("M1", _end(n, ys[k])) for k, n in lengths.items()),
                terminal=False,
            ),
        ],
        [],
        DBU,
    )


def _f3() -> ResistanceGraph:
    """Shared strip H, ideal junction J, four S branches to ideal RAIL."""
    ys = [Y_STEP * (i + 1) for i in range(4)]
    s_polys = tuple(_strip(H_LEN, y, S_LEN) for y in ys)
    return build_resistance_graph(
        [RoleInput("M1", RS, (_strip(0, 0, H_LEN), *s_polys))],
        [
            PortInput("H_FREE", (("M1", _end(0, 0)),)),
            PortInput(
                "J",
                (("M1", _end(H_LEN, 0)), *(("M1", _end(H_LEN, y)) for y in ys)),
                terminal=False,
            ),
            PortInput("RAIL", tuple(("M1", _end(H_LEN + S_LEN, y)) for y in ys)),
        ],
        [],
        DBU,
    )


def _solve_f4() -> ResistanceGraph:
    """F4: stacked S/S with ideal vias at both ends. The ideal via is expressed
    as each terminal port reaching both levels (one contracted node), so the
    terminal *is* the via endpoint as in the 3.D4 realisation; a ``LandingInput``
    would add its own port, joined to the terminal only through the strip's
    end piece."""
    return build_resistance_graph(
        [
            RoleInput("M1", RS, (_strip(0, 0, S_LEN),)),
            RoleInput("M2", RS, (_strip(0, 0, S_LEN),)),
        ],
        [
            PortInput("A", (("M1", _end(0, 0)), ("M2", _end(0, 0)))),
            PortInput("B", (("M1", _end(S_LEN, 0)), ("M2", _end(S_LEN, 0)))),
        ],
        [],
        DBU,
    )


def _solve_f4v(rv: float = 10.0) -> ResistanceGraph:
    """Hand-built F4v: two 100 ohm strips, finite ``Rv`` via at each end, the
    terminals being the level-1 end nodes."""
    nodes = tuple(GraphNode(f"port:{i}", "port", None) for i in range(4))
    edges = (
        GraphEdge("port:0", "port:1", 100.0, "in_plane"),  # level 1: A - B
        GraphEdge("port:0", "port:2", rv, "via"),  # A - level 2 start
        GraphEdge("port:1", "port:3", rv, "via"),  # B - level 2 end
        GraphEdge("port:2", "port:3", 100.0, "in_plane"),  # level 2
    )
    return ResistanceGraph("ok", nodes, edges, (("A", "port:0"), ("B", "port:1")), ())


def _solve_f5(r_cut: float) -> ResistanceGraph:
    """F5/F5v: level-1 S, one landing, level-2 S; terminals at the outer ends."""
    return build_resistance_graph(
        [
            RoleInput("M1", RS, (_strip(0, 0, S_LEN),)),
            RoleInput("M2", RS, (_strip(S_LEN, 0, S_LEN),)),
        ],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("B", (("M2", _end(2 * S_LEN, 0)),)),
        ],
        [LandingInput("M1", "M2", _end(S_LEN, 0), 0.04, r_cut)],
        DBU,
    )


def _f6() -> ResistanceGraph:
    return build_resistance_graph(
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


def _f9() -> ResistanceGraph:
    return build_resistance_graph(
        [RoleInput("M1", RS, (_strip(0, 0, H_LEN), _strip(0, 10 * UM, S_LEN)))],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("B", (("M1", _end(0, 10 * UM)),)),
            PortInput("C", (("M1", _end(S_LEN, 10 * UM)),)),
        ],
        [],
        DBU,
    )


def _f2v(branches: int, rv: float = 10.0, strip: float = 100.0) -> ResistanceGraph:
    """Hand-built F2v: RAIL_A -Rv- b_i -S- RAIL_B for each branch (graph level,
    the via is the finite Rv edge)."""
    nodes = [GraphNode("port:0", "port", None), GraphNode("port:1", "port", None)]
    edges = []
    for i in range(branches):
        nodes.append(GraphNode(f"port:{i + 2}", "port", None))
        edges.append(GraphEdge("port:0", f"port:{i + 2}", rv, "via"))
        edges.append(GraphEdge(f"port:{i + 2}", "port:1", strip, "in_plane"))
    return ResistanceGraph(
        "ok",
        tuple(nodes),
        tuple(edges),
        (("RAIL_A", "port:0"), ("RAIL_B", "port:1")),
        (),
    )


def test_f1_single_strip_is_100_ohm_from_one_solve():
    for pieces in (1, 2, 7):
        matrix = terminal_resistance_matrix(_f1(pieces))
        assert matrix.status == "ok"
        assert matrix.terminals == ("A", "B")
        assert matrix.pair_resistance_ohm[0][0] == 0.0
        assert matrix.pair_resistance_ohm[1][1] == 0.0
        assert matrix.pair_resistance_ohm[0][1] == pytest.approx(100.0, rel=TOL)
        assert matrix.pair_resistance_ohm[1][0] == matrix.pair_resistance_ohm[0][1]
        assert matrix.diagnostics == ()
        assert matrix.solves == 1


def test_effective_resistance_uses_a_single_pad_and_unit_injection(monkeypatch):
    calls = []
    real = erg.solve_ir_drop

    def spy(node_ids, edges, **kwargs):
        calls.append(
            (dict(kwargs.get("pads") or {}), dict(kwargs.get("injections") or {}))
        )
        return real(node_ids, edges, **kwargs)

    monkeypatch.setattr(erg, "solve_ir_drop", spy)
    graph = _f1(4)
    nodes = dict(graph.terminals)
    assert effective_resistance(graph, "A", "B") == pytest.approx(100.0, rel=TOL)
    assert calls == [({nodes["B"]: 0.0}, {nodes["A"]: 1.0})]
    assert effective_resistance(graph, "A", "A") == 0.0


@pytest.mark.parametrize(("n", "expected"), [(2, 50.0), (4, 25.0), (8, 12.5)])
def test_f2_parallel_branches_divide_by_n(n, expected):
    graph = _rails([S_LEN] * n)
    matrix = terminal_resistance_matrix(graph)
    assert matrix.solves == 1
    assert _two_terminal(graph, "RAIL_A", "RAIL_B") == pytest.approx(expected, rel=TOL)
    assert effective_resistance(graph, "RAIL_A", "RAIL_B") == pytest.approx(
        expected, rel=TOL
    )
    assert effective_resistance(graph, "RAIL_B", "RAIL_A") == pytest.approx(
        expected, rel=TOL
    )


@pytest.mark.parametrize(("n", "expected"), [(3, 110 / 3), (4, 27.5)])
def test_f2v_branch_vias_in_series(n, expected):
    graph = _f2v(n)
    assert _two_terminal(graph, "RAIL_A", "RAIL_B") == pytest.approx(expected, rel=TOL6)
    assert terminal_resistance_matrix(graph).solves == 1


def test_f3_shared_strip_then_parallel_branches_is_75_ohm():
    graph = _f3()
    assert _two_terminal(graph, "H_FREE", "RAIL") == pytest.approx(75.0, rel=TOL)
    assert terminal_resistance_matrix(graph).solves == 1


def test_f4_and_f4v_stacked_parallel_levels():
    f4, f4v = _solve_f4(), _solve_f4v()
    assert _two_terminal(f4, "A", "B") == pytest.approx(50.0, rel=TOL)
    assert _two_terminal(f4v, "A", "B") == pytest.approx(12000 / 220, rel=TOL6)
    assert terminal_resistance_matrix(f4).solves == 1
    assert terminal_resistance_matrix(f4v).solves == 1


def test_f4_and_f4v_through_landings_share_the_terminal_node():
    """A landing end on a terminal's own patch reuses the terminal node, so the
    landing form of F4/F4v gives the same resistance as the hand-built graph."""
    f4, f4v = _f4(0.0), _f4(10.0)
    assert _two_terminal(f4, "A", "B") == pytest.approx(50.0, rel=TOL)
    assert _two_terminal(f4v, "A", "B") == pytest.approx(12000 / 220, rel=TOL6)
    assert terminal_resistance_matrix(f4v).solves == 1


def test_f5_and_f5v_single_landing_is_series():
    f5, f5v = _solve_f5(0.0), _solve_f5(10.0)
    assert _two_terminal(f5, "A", "B") == pytest.approx(200.0, rel=TOL)
    assert _two_terminal(f5v, "A", "B") == pytest.approx(210.0, rel=TOL)
    assert terminal_resistance_matrix(f5).solves == 1
    assert terminal_resistance_matrix(f5v).solves == 1


def test_f6_overlap_without_via_is_off_the_current_path():
    matrix = terminal_resistance_matrix(_f6(), dbu=DBU)
    assert matrix.status == "ok"
    assert matrix.pair_resistance_ohm[0][1] == pytest.approx(100.0, rel=TOL)
    assert matrix.solves == 1
    [diag] = matrix.diagnostics
    assert diag.code == "floating_component"
    assert diag.detail["count"] == 1
    assert diag.detail["area_dbu2"] == S_LEN * WIDTH
    assert diag.detail["area_um2"] == pytest.approx(40.0)


def test_f7_unequal_parallel_branches():
    f7a = _rails([S_LEN, 60 * UM])
    f7b = _rails([S_LEN, 2 * S_LEN, 4 * S_LEN])
    assert _two_terminal(f7a, "RAIL_A", "RAIL_B") == pytest.approx(75.0, rel=TOL)
    assert _two_terminal(f7b, "RAIL_A", "RAIL_B") == pytest.approx(400 / 7, rel=TOL6)
    assert terminal_resistance_matrix(f7a).solves == 1
    assert terminal_resistance_matrix(f7b).solves == 1


def test_f8_floating_star_matrix_and_two_solves():
    matrix = terminal_resistance_matrix(_star_arms())
    assert matrix.status == "ok"
    assert matrix.terminals == ("A", "B", "C")
    m = matrix.pair_resistance_ohm
    assert [m[i][i] for i in range(3)] == [0.0, 0.0, 0.0]
    assert m[0][1] == pytest.approx(150.0, rel=TOL6)
    assert m[0][2] == pytest.approx(250.0, rel=TOL6)
    assert m[1][2] == pytest.approx(200.0, rel=TOL6)
    assert all(m[i][j] == m[j][i] for i in range(3) for j in range(3))
    assert matrix.solves == 2
    assert matrix.diagnostics == ()


def test_f8_tied_terminal_changes_the_result_without_changing_topology():
    graph = _star_arms()
    assert effective_resistance(graph, "A", "B", ties=[("B", "C")]) == pytest.approx(
        137.5, rel=TOL6
    )
    matrix = terminal_resistance_matrix(graph, ties=[("B", "C")])
    m = matrix.pair_resistance_ohm
    assert m[0][1] == pytest.approx(137.5, rel=TOL6)
    assert m[0][2] == pytest.approx(137.5, rel=TOL6)
    assert m[1][2] == 0.0  # contracted terminals: exactly zero
    assert matrix.solves == 2
    with pytest.raises(ValueError, match="unknown terminal"):
        terminal_resistance_matrix(graph, ties=[("B", "Z")])
    with pytest.raises(ValueError, match="unknown terminal"):
        effective_resistance(graph, "A", "Z")


def test_f9_disconnected_terminals_get_null_one_solve_exact_diagnostic():
    matrix = terminal_resistance_matrix(_f9())
    assert matrix.status == "ok"
    assert matrix.terminals == ("A", "B", "C")
    m = matrix.pair_resistance_ohm
    assert [m[i][i] for i in range(3)] == [0.0, 0.0, 0.0]
    assert m[1][2] == pytest.approx(100.0, rel=TOL) and m[2][1] == m[1][2]
    assert (m[0][1], m[0][2], m[1][0], m[2][0]) == (None, None, None, None)
    assert matrix.solves == 1  # {A} costs none; {B, C} costs one
    [diag] = matrix.diagnostics
    assert diag.code == "terminals_disconnected"
    assert diag.detail == {"groups": [["A"], ["B", "C"]]}
    # H carries terminal A: a dead end, not a floating component.
    assert all(d.code != "floating_component" for d in matrix.diagnostics)
    assert effective_resistance(_f9(), "A", "B") is None
    assert effective_resistance(_f9(), "B", "C") == pytest.approx(100.0, rel=TOL)
    text = json.dumps(matrix.as_dict(), allow_nan=False)
    assert "null" in text


def test_grouping_call_is_not_counted_as_an_injection_solve(monkeypatch):
    real = erg.solve_ir_drop
    calls = []

    def spy(node_ids, edges, **kwargs):
        calls.append(dict(kwargs.get("injections") or {}))
        return real(node_ids, edges, **kwargs)

    monkeypatch.setattr(erg, "solve_ir_drop", spy)
    matrix = terminal_resistance_matrix(_star_arms())
    assert calls[0] == {}  # the pad-less, injection-less grouping call
    assert len([c for c in calls if c]) == matrix.solves == 2


def test_zero_resistance_path_is_exactly_zero():
    graph = _f1(3)
    matrix = terminal_resistance_matrix(graph, ties=[("A", "B")])
    assert matrix.pair_resistance_ohm[0][1] == 0.0
    assert effective_resistance(graph, "A", "B", ties=[("A", "B")]) == 0.0


def test_not_converged_component_is_singular_system_with_finite_json():
    matrix = terminal_resistance_matrix(_f1(7), max_iterations=1)
    assert matrix.status == "unsupported"
    assert matrix.pair_resistance_ohm == ()
    assert [d.code for d in matrix.diagnostics] == ["singular_system"]
    json.dumps(matrix.as_dict(), allow_nan=False)
    assert effective_resistance(_f1(7), "A", "B", max_iterations=1) is None


def test_unsupported_graph_and_too_few_terminals_are_reported_not_solved():
    bad = build_resistance_graph(
        [RoleInput("M1", None, (_strip(0, 0, S_LEN),))], [], [], DBU
    )
    assert bad.status == "unsupported"
    matrix = terminal_resistance_matrix(bad)
    assert matrix.status == "unsupported"
    assert [d.code for d in matrix.diagnostics] == ["sheet_resistance_unknown"]
    one = build_resistance_graph(
        [RoleInput("M1", RS, (_strip(0, 0, S_LEN),))],
        [PortInput("A", (("M1", _end(0, 0)),))],
        [],
        DBU,
    )
    skipped = terminal_resistance_matrix(one)
    assert skipped.status == "skipped"
    assert [d.code for d in skipped.diagnostics] == ["too_few_terminals"]
    assert skipped.solves == 0


def test_matrix_is_byte_identical_across_runs():
    first = json.dumps(terminal_resistance_matrix(_star_arms()).as_dict())
    second = json.dumps(terminal_resistance_matrix(_star_arms()).as_dict())
    assert first == second


def test_monotonicity_holds_only_for_the_bounded_relations():
    f1 = _two_terminal(_f1(2), "A", "B")
    # Same endpoints, same boundary conditions, an extra passive parallel branch.
    assert _two_terminal(_rails([S_LEN] * 2), "RAIL_A", "RAIL_B") <= f1 * (1 + TOL)
    assert _two_terminal(_solve_f4(), "A", "B") <= f1 * (1 + TOL)
    assert _two_terminal(_rails([S_LEN, 60 * UM]), "RAIL_A", "RAIL_B") <= f1 * (1 + TOL)
    f2v3 = _two_terminal(_f2v(3), "RAIL_A", "RAIL_B")
    f2v4 = _two_terminal(_f2v(4), "RAIL_A", "RAIL_B")
    assert f2v4 <= f2v3 * (1 + TOL)
    # Not general: changing endpoints can raise R (F8 R_AC > R_AB), and adding a
    # series element raises R (F5v > F5).
    star = terminal_resistance_matrix(_star_arms()).pair_resistance_ohm
    assert star[0][2] > star[0][1]
    assert _two_terminal(_solve_f5(10.0), "A", "B") > _two_terminal(
        _solve_f5(0.0), "A", "B"
    )
