"""Solve-level tests for the resistance graph (issue #2981, Stage 3.D5
increment 2): the terminal-pair effective-resistance matrix over the same
3.D4 matrix rows the builder tests draw, asserted against the 3.D4 analytic
results, plus the monotonicity relations 3.D4 scopes.

Split out of ``tests/test_extract_resistance_graph.py`` (issue #6917) with
no change to any fixture or expectation. The builder half keeps the shared
3.D4 fixtures; this module imports them directly, the established
cross-test-module pattern (``tests/test_abstract_substrate_pin_lef_merge.py``
imports ``test_extract`` the same way).
"""

from __future__ import annotations

import json

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
from test_extract_resistance_graph import (
    DBU,
    H_LEN,
    RS,
    S_LEN,
    TOL,
    UM,
    WIDTH,
    _end,
    _f1,
    _f4,
    _strip,
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


def _landing_pair_resistance(landing: kdb.Box) -> float:
    graph = build_resistance_graph(
        [
            RoleInput("M1", RS, (_strip(0, 0, S_LEN),)),
            RoleInput("M2", RS, (_strip(0, 0, S_LEN),)),
        ],
        [
            PortInput("A", (("M1", _end(0, 0)),)),
            PortInput("B", (("M2", _end(S_LEN, 0)),)),
        ],
        [LandingInput("M1", "M2", landing, 0.04, 0.0)],
        DBU,
    )
    assert graph.status == "ok"
    matrix = terminal_resistance_matrix(graph, dbu=DBU)
    assert matrix.status == "ok"
    return matrix.pair_resistance_ohm[0][1]


def test_interior_landing_does_not_double_conductance_across_it():
    """A landing wholly inside both strips attaches by its footprint only. The
    full-height landing at the same x span is the analytic reference (the
    patch centre at 7 um: 20 um of strip = 100 ohm); insetting the landing from
    the outline only
    adds current crowding, so the result is >= the reference, whereas half
    edges to the abutting neighbours would push it below."""
    full = kdb.Box(6 * UM, 0, 8 * UM, WIDTH)
    inset = kdb.Box(6 * UM, UM // 2, 8 * UM, WIDTH - UM // 2)
    r_full = _landing_pair_resistance(full)
    r_inset = _landing_pair_resistance(inset)
    assert r_full == pytest.approx(RS * 20 / 2, rel=1e-9)
    assert r_full - TOL <= r_inset < r_full * 1.6


def test_interior_landing_attaches_only_overlapped_pieces():
    inset = kdb.Box(6 * UM, UM // 2, 8 * UM, WIDTH - UM // 2)
    graph = build_resistance_graph(
        [RoleInput("M1", RS, (_strip(0, 0, S_LEN),))],
        [PortInput("A", (("M1", inset),))],
        [],
        DBU,
    )
    port_edges = [
        e for e in graph.edges if e.a.startswith("port") or e.b.startswith("port")
    ]
    assert len(port_edges) == 1
    assert port_edges[0].resistance_ohm == 0.0
