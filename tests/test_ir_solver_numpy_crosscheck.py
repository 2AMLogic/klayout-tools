"""Cross-check the pure-Python numerical surfaces against **numpy's dense
direct solvers** (issue #2276).

``klayout-tools``'s runtime dependency set is deliberately just
``klayout``/``jsonschema`` (see ``src/klayout_tools/ir_solver.py``'s own
docstring), so the numpy cross-checks below cannot run on the stdlib-only
surface: the whole module skips via ``pytest.importorskip("numpy")`` and a
stdlib-only host is expected to see exactly that skip -- a *declared*
unavailability, never a failure and never a false pass. The one place the
full-strength assertions are allowed to run is the ``test-numerical`` CI
job, which installs the locked ``numerical`` dependency group
(``uv sync --locked --group numerical``); see
``docs/guides/dependency-gated-ci.md`` for the skip/refuse/fail convention
this split implements.

What is cross-checked, and against what oracle:

- ``ir_solver.solve_ir_drop`` (the conjugate-gradient engine behind
  ``klt power``) against ``numpy.linalg.solve`` on the *same* Dirichlet
  system, assembled independently here in dense form: the zero-resistance
  supernode merge (union-find there, short-edge flood fill here), the pad
  elimination, and the solve-as-deviation-from-the-pad-reference convention
  are all reimplemented rather than imported. The closed-form networks from
  ``tests/test_ir_solver.py`` reappear (so the oracle itself is re-validated
  against physics before it is trusted against the solver), plus a
  randomized sparse SPD family -- random connected resistor graphs with
  zero-resistance shorts, 1-3 pads on distinct supernodes, random current
  injections, occasionally a pad-less detached island -- the machinery whose
  hand-solvable coverage is necessarily sparse.
- ``sim.py``'s Monte Carlo statistics (``_percentile``'s linearly
  interpolated quantiles, the Bessel-corrected stddev, and
  ``_sample_statistics`` end to end) against ``numpy.percentile`` /
  ``numpy.mean`` / ``numpy.std``.

**Stated tolerance.** Both implementations solve the *same* linear system,
so unlike ``tests/test_ir_solver.py``'s analytic tolerances (dominated by
closed-form truncation) the only budget here is floating-point round-off
plus the conjugate-gradient iteration's own declared convergence
(``ir_solver.DEFAULT_TOLERANCE = 1e-12`` relative residual, amplified by
the system's condition number). The comparisons are therefore asserted at
``rtol=1e-6`` with an absolute floor of ``1e-8`` x the network's own
deviation/current scale -- orders of magnitude tighter than any physical
tolerance, and the solver's *reported* residual is separately asserted
``<= 1e-10`` so the budget's premise is checked, not assumed.

The randomized family is drawn from Python's own ``random.Random`` with
fixed integer seeds -- deliberately not ``numpy.random``, so the random
input never shares an implementation (or a version) with the oracle it
feeds, and the draws stay identical across numpy upgrades.
"""

from __future__ import annotations

import random
import statistics

import pytest

from klayout_tools.ir_solver import solve_ir_drop, worst_deviation
from klayout_tools.sim import DEFAULT_MC_QUANTILES, _percentile, _sample_statistics

# Whole-module gate: numpy is the oracle, so without it there is nothing to
# compare against and this module's contract with a stdlib-only host is a
# clean, *declared* skip (issue #2276).
np = pytest.importorskip("numpy")

#: Relative tolerance for solver-vs-oracle agreement on the *solved*
#: quantities (deviations, edge currents, pad current). See the module
#: docstring's "Stated tolerance" for why this budget is round-off plus CG
#: convergence, not model error.
CROSS_CHECK_RTOL = 1e-6

#: Absolute floor for the same comparisons, scaled by each network's own
#: deviation/current magnitude -- keeps nodes whose solved quantity is (near)
#: zero held to the same *absolute* precision instead of a vacuous relative
#: one.
CROSS_CHECK_ATOL_SCALE = 1e-8

#: The solver reports its achieved relative residual per component; this is
#: a full-strength demand that the CG iteration actually delivered the
#: precision the comparison budget above assumes, not a hope.
REPORTED_RESIDUAL_MAX = 1e-10


# --- Independent dense oracle ------------------------------------------------


def _short_supernode_labels(node_list: list[str], edges: list[dict]) -> dict[str, int]:
    """Map each node to its zero-resistance supernode index.

    The same merge ``ir_solver.solve_ir_drop`` performs with union-find,
    reimplemented as a flood fill over the short edges so the oracle stays an
    independent implementation, not a mirror. Labels are unique per supernode
    but not necessarily consecutive: a node absorbed by an earlier flood fill
    never starts a label of its own.
    """
    short_neighbours: dict[str, list[str]] = {node: [] for node in node_list}
    for edge in edges:
        if float(edge["resistance_ohm"]) == 0.0:
            short_neighbours[edge["from"]].append(edge["to"])
            short_neighbours[edge["to"]].append(edge["from"])
    labels: dict[str, int] = {}
    for node in node_list:
        if node in labels:
            continue
        label = len(labels)
        labels[node] = label
        stack = [node]
        while stack:
            current = stack.pop()
            for neighbour in short_neighbours[current]:
                if neighbour not in labels:
                    labels[neighbour] = label
                    stack.append(neighbour)
    return labels


def _reference_matrices(
    node_list: list[str],
    edges: list[dict],
    pads: dict[str, float],
    injections: dict[str, float],
) -> tuple[dict[str, int], int, np.ndarray, np.ndarray, dict[int, float]]:
    """The dense Laplacian over supernodes, the per-supernode injection
    vector, and the pad voltages keyed by supernode -- the oracle's version
    of ``solve_ir_drop``'s adjacency/injection/pad bookkeeping."""
    labels = _short_supernode_labels(node_list, edges)
    # Size the arrays off the largest label, not the label count (see
    # _short_supernode_labels on why they can differ).
    super_count = max(labels.values()) + 1

    conductance = np.zeros((super_count, super_count))
    for edge in edges:
        resistance = float(edge["resistance_ohm"])
        if resistance == 0.0:
            continue
        a, b = labels[edge["from"]], labels[edge["to"]]
        if a == b:
            # A self-loop (or a loop closed by a short): carries no current.
            continue
        g = 1.0 / resistance
        conductance[a, a] += g
        conductance[b, b] += g
        conductance[a, b] -= g
        conductance[b, a] -= g

    injection = np.zeros(super_count)
    for node_id, value in injections.items():
        injection[labels[node_id]] += float(value)

    pad_voltage: dict[int, float] = {}
    for node_id, voltage in pads.items():
        pad_voltage.setdefault(labels[node_id], voltage)

    return labels, super_count, conductance, injection, pad_voltage


def _reference_components(
    conductance: np.ndarray,
) -> tuple[list[list[int]], list[int]]:
    """Connected components over the supernode graph, as (sorted member lists
    in first-seen order, per-supernode component index) -- the same ordering
    discipline ``solve_ir_drop`` uses for its per-component reports."""
    super_count = len(conductance)
    neighbours: list[list[int]] = [[] for _ in range(super_count)]
    for a in range(super_count):
        for b in range(a + 1, super_count):
            if conductance[a, b] != 0.0:
                neighbours[a].append(b)
                neighbours[b].append(a)

    component_of = [-1] * super_count
    component_supers: list[list[int]] = []
    for start in range(super_count):
        if component_of[start] != -1:
            continue
        index = len(component_supers)
        component_of[start] = index
        members = [start]
        stack = [start]
        while stack:
            u = stack.pop()
            for v in neighbours[u]:
                if component_of[v] == -1:
                    component_of[v] = index
                    members.append(v)
                    stack.append(v)
        component_supers.append(sorted(members))
    return component_supers, component_of


def _component_unknowns(
    members: list[int], pad_voltage: dict[int, float]
) -> tuple[float, dict[int, float], list[int], dict[int, int]]:
    """The Dirichlet boundary for one component: the reference voltage (the
    largest pad voltage), the pinned supernodes, and the unknowns left to
    solve for -- mirroring ``solve_ir_drop``'s elimination of pad nodes into
    the right-hand side."""
    pad_members = [u for u in members if u in pad_voltage]
    reference = max(pad_voltage[u] for u in pad_members)
    pinned = {u: pad_voltage[u] for u in pad_members}
    unknowns = [u for u in members if u not in pinned]
    local = {u: i for i, u in enumerate(unknowns)}
    return reference, pinned, unknowns, local


def _solve_component_deviations(
    members: list[int],
    conductance: np.ndarray,
    injection: np.ndarray,
    pad_voltage: dict[int, float],
):
    """Densely solve one component's reduced system for the deviations from
    its reference voltage, or return ``None`` when the component has no pad
    (genuinely no operating point -- never guessed)."""
    if not [u for u in members if u in pad_voltage]:
        return None
    reference, pinned, unknowns, local = _component_unknowns(members, pad_voltage)
    pad_index = list(pinned)
    pad_w = np.array([pinned[v] - reference for v in pad_index])
    rhs = injection[unknowns] - conductance[np.ix_(unknowns, pad_index)] @ pad_w
    matrix = conductance[np.ix_(unknowns, unknowns)]
    w = np.linalg.solve(matrix, rhs) if unknowns else np.zeros(0)
    return reference, pinned, unknowns, local, w


def _pad_current(
    pinned: dict[int, float],
    injection: np.ndarray,
    conductance: np.ndarray,
    super_voltage: list[float | None],
) -> float:
    """What the pads together deliver into their component: KCL at every
    pinned supernode reads ``P_u + I_u = sum_v g_uv (V_u - V_v)``, so
    ``P_u`` is the resistive outflow minus any injection attached to that
    same supernode -- the same conservation law
    ``ir_solver.solve_ir_drop`` reports per component."""
    pad_current = 0.0
    for u, v_u in pinned.items():
        pad_current -= injection[u]
        for v in range(len(conductance)):
            # conductance[u, v] is the Laplacian *matrix* entry (-g for a
            # neighbour, +degree on the diagonal); the resistive outflow is
            # the sum over positive edge conductances, hence the minus.
            g_uv = conductance[u, v]
            if v != u and g_uv:
                pad_current -= g_uv * (v_u - super_voltage[v])
    return pad_current


def _component_nodes(
    node_list: list[str], labels: dict[str, int], component_of: list[int], index: int
) -> frozenset[str]:
    """Every original node belonging to component ``index``."""
    return frozenset(node for node in node_list if component_of[labels[node]] == index)


def _reference_edge_currents(
    edges: list[dict], voltages: dict[str, float | None]
) -> dict[str, float | None]:
    """Signed ``from -> to`` branch currents from the oracle's voltages,
    ``None`` for a shorted edge (not recoverable from node voltages) or an
    unsolved component."""
    shorted = {edge["id"] for edge in edges if float(edge["resistance_ohm"]) == 0.0}
    edge_currents: dict[str, float | None] = {}
    for edge in edges:
        if edge["id"] in shorted:
            edge_currents[edge["id"]] = None
            continue
        v_a, v_b = voltages[edge["from"]], voltages[edge["to"]]
        edge_currents[edge["id"]] = (
            None
            if v_a is None or v_b is None
            else (v_a - v_b) / float(edge["resistance_ohm"])
        )
    return edge_currents


def _numpy_reference(
    node_list: list[str],
    edges: list[dict],
    pads: dict[str, float],
    injections: dict[str, float],
) -> dict:
    """Solve the same network with ``numpy.linalg.solve``, dense and direct.

    Assembles the Dirichlet-reduced nodal system from scratch (per connected
    component, deviations from the largest pad voltage) and returns the same
    shapes ``solve_ir_drop`` reports, so the comparison below is 1:1.
    """
    labels, super_count, conductance, injection, pad_voltage = _reference_matrices(
        node_list, edges, pads, injections
    )
    component_supers, component_of = _reference_components(conductance)

    super_voltage: list[float | None] = [None] * super_count
    super_deviation: list[float | None] = [None] * super_count
    pad_currents: dict[frozenset[str], float] = {}
    for component_index, members in enumerate(component_supers):
        solved = _solve_component_deviations(
            members, conductance, injection, pad_voltage
        )
        if solved is None:
            continue
        reference, pinned, unknowns, local, w = solved
        for u in members:
            deviation = pinned[u] - reference if u in pinned else w[local[u]]
            super_voltage[u] = reference + deviation
            super_deviation[u] = deviation
        member_nodes = _component_nodes(
            node_list, labels, component_of, component_index
        )
        pad_currents[member_nodes] = _pad_current(
            pinned, injection, conductance, super_voltage
        )

    voltages = {node: super_voltage[labels[node]] for node in node_list}
    deviations = {node: super_deviation[labels[node]] for node in node_list}
    shorted_edge_ids = [
        edge["id"] for edge in edges if float(edge["resistance_ohm"]) == 0.0
    ]
    return {
        "voltages": voltages,
        "deviations": deviations,
        "edge_currents": _reference_edge_currents(edges, voltages),
        "shorted_edge_ids": shorted_edge_ids,
        "pad_currents": pad_currents,
    }


# --- Comparison harness ------------------------------------------------------


def _approx(actual, expected, scale):
    """``pytest.approx`` at the module's stated cross-check tolerance."""
    return actual == pytest.approx(
        expected, rel=CROSS_CHECK_RTOL, abs=CROSS_CHECK_ATOL_SCALE * max(scale, 1e-15)
    )


def _max_abs(values: dict) -> float:
    """Largest magnitude among the non-``None`` entries -- the scale the
    absolute comparison floor is anchored to."""
    return max((abs(v) for v in values.values() if v is not None), default=0.0)


def _assert_residuals_reported_small(result: dict) -> None:
    """Every solved component's own reported residual -- recomputed exactly
    by the solver, not carried from the iteration -- must have delivered the
    precision the cross-check tolerance assumes."""
    for component in result["components"]:
        if component["solved"]:
            assert component["residual"] <= REPORTED_RESIDUAL_MAX


def _assert_node_values_match(result: dict, reference: dict, scale: float) -> None:
    """Node voltages and deviations: ``None``ness must agree exactly (an
    unsolved component is reported, never guessed), solved values to the
    stated tolerance."""
    for node_id, expected in reference["voltages"].items():
        actual = result["voltages"][node_id]
        assert (actual is None) == (expected is None)
        if expected is not None:
            assert _approx(actual, expected, scale)
        expected_deviation = reference["deviations"][node_id]
        actual_deviation = result["deviations"][node_id]
        assert (actual_deviation is None) == (expected_deviation is None)
        if expected_deviation is not None:
            assert _approx(actual_deviation, expected_deviation, scale)


def _assert_edge_currents_match(result: dict, reference: dict, scale: float) -> None:
    """Per-edge branch currents, ``None``ness included."""
    for edge_id, expected in reference["edge_currents"].items():
        actual = result["edge_currents"][edge_id]
        assert (actual is None) == (expected is None)
        if expected is not None:
            assert _approx(actual, expected, scale)


def _assert_pads_and_shorts_match(
    result: dict, reference: dict, current_scale: float, deviation_scale: float
) -> None:
    """The two aggregate reports: pad current conservation per solved
    component, the shorted-edge list, and ``worst_deviation`` against the
    oracle's own worst magnitude."""
    assert sorted(result["shorted_edge_ids"]) == sorted(reference["shorted_edge_ids"])
    for component in result["components"]:
        if not component["solved"]:
            continue
        expected = reference["pad_currents"][frozenset(component["node_ids"])]
        assert _approx(component["pad_current_a"], expected, current_scale)
    _, worst = worst_deviation(result["deviations"])
    if deviation_scale > 0.0:
        assert _approx(worst, deviation_scale, deviation_scale)


def _assert_matches_reference(result: dict, reference: dict) -> None:
    """Compare a ``solve_ir_drop`` result 1:1 against the dense oracle."""
    assert set(result["voltages"]) == set(reference["voltages"])
    _assert_residuals_reported_small(result)
    deviation_scale = _max_abs(reference["deviations"])
    _assert_node_values_match(result, reference, deviation_scale)
    current_scale = _max_abs(reference["edge_currents"])
    _assert_edge_currents_match(result, reference, current_scale)
    _assert_pads_and_shorts_match(result, reference, current_scale, deviation_scale)


# --- Closed-form networks: the oracle is validated against physics first -----


def _chain(n_segments: int, resistance_ohm: float = 1.0):
    """A 1-D ladder: ``n_segments`` resistors in series, nodes ``n0..nN``."""
    nodes = [f"n{i}" for i in range(n_segments + 1)]
    edges = [
        {
            "id": f"e{i}",
            "from": f"n{i}",
            "to": f"n{i + 1}",
            "resistance_ohm": resistance_ohm,
        }
        for i in range(n_segments)
    ]
    return nodes, edges


def test_series_ladder_solver_and_numpy_agree_with_ohms_law():
    """The ``tests/test_ir_solver.py`` ladder, re-solved by both sides: numpy
    must reproduce Ohm's law exactly (validating the oracle), and the
    conjugate-gradient solver must match numpy's direct solve."""
    n = 100
    current_a = 1e-3
    nodes, edges = _chain(n)
    pads = {"n0": 1.8}
    injections = {f"n{n}": -current_a}

    reference = _numpy_reference(nodes, edges, pads, injections)
    for k in range(n + 1):
        expected = 1.8 - current_a * k
        assert reference["voltages"][f"n{k}"] == pytest.approx(expected, rel=1e-12)

    result = solve_ir_drop(nodes, edges, pads=pads, injections=injections)
    _assert_matches_reference(result, reference)


def test_double_fed_rail_solver_and_numpy_agree():
    """A uniformly loaded rail strapped at both ends at *different* pad
    voltages -- the Dirichlet boundary with a non-zero reference offset, in
    both implementations at once."""
    n = 40
    current_a = 1e-4
    nodes, edges = _chain(n)
    pads = {"n0": 1.80, f"n{n}": 1.79}
    injections = {f"n{k}": -current_a for k in range(1, n)}

    reference = _numpy_reference(nodes, edges, pads, injections)
    result = solve_ir_drop(nodes, edges, pads=pads, injections=injections)
    _assert_matches_reference(result, reference)

    # The reference convention itself: deviations are measured against the
    # *largest* pad voltage in the component.
    assert result["components"][0]["reference_voltage_v"] == pytest.approx(1.80)


def test_balanced_wheatstone_bridge_solver_and_numpy_agree():
    """The balanced bridge (zero bridge current) re-solved by both sides."""
    nodes = ["top", "left", "right", "bottom"]
    edges = [
        {"id": "r1", "from": "top", "to": "left", "resistance_ohm": 10.0},
        {"id": "r2", "from": "left", "to": "bottom", "resistance_ohm": 20.0},
        {"id": "r3", "from": "top", "to": "right", "resistance_ohm": 30.0},
        {"id": "r4", "from": "right", "to": "bottom", "resistance_ohm": 60.0},
        {"id": "bridge", "from": "left", "to": "right", "resistance_ohm": 7.0},
    ]
    pads = {"top": 5.0, "bottom": 0.0}

    reference = _numpy_reference(nodes, edges, pads, {})
    assert reference["edge_currents"]["bridge"] == pytest.approx(0.0, abs=1e-15)
    result = solve_ir_drop(nodes, edges, pads=pads, injections={})
    _assert_matches_reference(result, reference)


# --- Supernode / Dirichlet machinery, deterministically ----------------------


def test_zero_resistance_short_merges_its_endpoints_like_the_oracle():
    """The ideal-short case from ``tests/test_ir_solver.py``: the shorted
    pair sits at one voltage, the short's own current is ``None``, and the
    dense oracle -- which merges the pair into one unknown -- agrees."""
    nodes = ["pad", "mid", "far"]
    edges = [
        {"id": "short", "from": "pad", "to": "mid", "resistance_ohm": 0.0},
        {"id": "wire", "from": "mid", "to": "far", "resistance_ohm": 4.0},
    ]
    pads = {"pad": 1.0}
    injections = {"far": -0.01}

    reference = _numpy_reference(nodes, edges, pads, injections)
    result = solve_ir_drop(nodes, edges, pads=pads, injections=injections)

    assert result["voltages"]["mid"] == pytest.approx(1.0, rel=1e-12)
    assert result["edge_currents"]["short"] is None
    _assert_matches_reference(result, reference)


def test_pad_and_injection_on_opposite_members_of_a_short():
    """A pad pinned on one member of a shorted pair and the load attached to
    the *other* member: the supernode machinery must route the injection into
    the merged node's KCL. The oracle pins the same merged supernode."""
    nodes = ["a", "b", "c", "d"]
    edges = [
        {"id": "short", "from": "a", "to": "b", "resistance_ohm": 0.0},
        {"id": "r1", "from": "b", "to": "c", "resistance_ohm": 2.5},
        {"id": "r2", "from": "c", "to": "d", "resistance_ohm": 7.5},
    ]
    pads = {"a": 1.2}
    injections = {"b": -0.01, "d": -0.02}

    reference = _numpy_reference(nodes, edges, pads, injections)
    result = solve_ir_drop(nodes, edges, pads=pads, injections=injections)

    # Both members sit at the pad voltage; the drop appears across r1/r2.
    assert result["voltages"]["a"] == pytest.approx(1.2, rel=1e-12)
    assert result["voltages"]["b"] == pytest.approx(1.2, rel=1e-12)
    _assert_matches_reference(result, reference)


# --- Randomized sparse SPD family -------------------------------------------


def _add_edge(edges: list[dict], a: str, b: str, resistance: float) -> None:
    """Append one resistor edge with a fresh unique id."""
    edges.append(
        {
            "id": f"e{len(edges)}",
            "from": a,
            "to": b,
            "resistance_ohm": resistance,
        }
    )


def _add_random_pads(
    rng: random.Random, nodes: list[str], labels: dict[str, int]
) -> dict[str, float]:
    """1-3 pads on nodes of *distinct* supernodes -- a second pad on one
    supernode would be a conflicting-voltage contradiction the generator has
    no business producing (the solver's contract tests cover that path)."""
    pads: dict[str, float] = {}
    used_supernodes: set[int] = set()
    order = list(range(len(nodes)))
    rng.shuffle(order)
    for i in order:
        if len(pads) >= rng.randint(1, 3):
            break
        if labels[nodes[i]] in used_supernodes:
            continue
        used_supernodes.add(labels[nodes[i]])
        pads[nodes[i]] = rng.uniform(0.5, 1.8)
    return pads


def _append_random_island(
    rng: random.Random,
    nodes: list[str],
    edges: list[dict],
    injections: dict[str, float],
    n_main: int,
) -> None:
    """Sometimes append a detached island with no pad: an un-strapped island
    is reported unsolved, and the oracle must agree it has no operating point
    rather than inventing one."""
    if rng.random() >= 0.3:
        return
    island_size = rng.randint(2, 4)
    island_nodes = [f"n{n_main + j}" for j in range(island_size)]
    nodes.extend(island_nodes)
    _add_edge(edges, island_nodes[0], island_nodes[1], rng.uniform(0.1, 10.0))
    if rng.random() < 0.5:
        injections[island_nodes[-1]] = -rng.uniform(1e-4, 1e-3)


def _random_network(rng: random.Random) -> tuple[list[str], list[dict], dict, dict]:
    """A random connected resistor network with zero-resistance shorts,
    pads on distinct supernodes, random injections, and (sometimes) a
    pad-less detached island -- the sparse SPD shape ``klt power`` actually
    sees, with none of it hand-solvable."""
    n_main = rng.randint(6, 30)
    nodes = [f"n{i}" for i in range(n_main)]
    edges: list[dict] = []

    # A spanning tree first, so every draw is connected; then random extra
    # edges (self-loops included on purpose -- both implementations must skip
    # them) and up to three zero-resistance shorts that merge supernodes.
    for i in range(1, n_main):
        _add_edge(edges, nodes[rng.randrange(i)], nodes[i], rng.uniform(0.1, 10.0))
    for _ in range(rng.randint(0, n_main // 2)):
        _add_edge(
            edges,
            nodes[rng.randrange(n_main)],
            nodes[rng.randrange(n_main)],
            rng.uniform(0.1, 10.0),
        )
    for _ in range(rng.randint(0, 3)):
        a, b = rng.randrange(n_main), rng.randrange(n_main)
        if a != b:
            _add_edge(edges, nodes[a], nodes[b], 0.0)

    pads = _add_random_pads(rng, nodes, _short_supernode_labels(nodes, edges))
    injections = {
        nodes[i]: rng.choice([-1.0, 1.0]) * rng.uniform(1e-4, 1e-2)
        for i in rng.sample(range(n_main), rng.randint(0, max(1, n_main // 3)))
    }
    _append_random_island(rng, nodes, edges, injections, n_main)
    return nodes, edges, pads, injections


@pytest.mark.parametrize("seed", range(30))
def test_randomized_sparse_spd_network_matches_numpy_reference(seed):
    """One seeded draw from the randomized family, solved by both sides."""
    rng = random.Random(2276_000 + seed)
    nodes, edges, pads, injections = _random_network(rng)

    reference = _numpy_reference(nodes, edges, pads, injections)
    result = solve_ir_drop(nodes, edges, pads=pads, injections=injections)
    _assert_matches_reference(result, reference)


# --- sim.py Monte Carlo statistics -------------------------------------------


def test_sim_percentile_matches_numpy_linear_quantiles():
    """``sim._percentile``'s docstring claims numpy's default ("inclusive"/
    linear) method; hold it to that claim across random samples and the full
    percentile range, including the degenerate p0/p100 and single-sample
    endpoints."""
    rng = random.Random(2276)
    percentiles = (0.0, 2.5, 5.0, 12.345, 50.0, 87.5, 95.0, 99.9, 100.0)
    for _ in range(25):
        values = [rng.gauss(1.0, 0.3) for _ in range(rng.randint(1, 200))]
        ordered = sorted(values)
        for percentile in percentiles:
            expected = float(np.percentile(values, percentile, method="linear"))
            assert _percentile(ordered, percentile) == pytest.approx(
                expected, rel=1e-12, abs=1e-15
            )


def test_sample_statistics_matches_numpy_moments_and_quantiles():
    """``sim._sample_statistics`` end to end: fmean, the Bessel-corrected
    stddev, min/max, and the configured quantiles all against numpy."""
    rng = random.Random(2277)
    for _ in range(10):
        values = [rng.gauss(3.3, 0.7) for _ in range(rng.randint(2, 150))]
        stats = _sample_statistics(
            values,
            errored=0,
            quantiles=DEFAULT_MC_QUANTILES,
            k_sigma=None,
            limits=None,
        )
        assert stats["mean"] == pytest.approx(float(np.mean(values)), rel=1e-12)
        assert stats["stddev"] == pytest.approx(
            float(np.std(values, ddof=1)), rel=1e-12
        )
        assert stats["stddev"] == pytest.approx(statistics.stdev(values), rel=1e-15)
        assert stats["min"] == pytest.approx(float(np.min(values)), rel=1e-15)
        assert stats["max"] == pytest.approx(float(np.max(values)), rel=1e-15)
        for percentile in DEFAULT_MC_QUANTILES:
            key = f"p{percentile:g}"
            expected = float(np.percentile(values, percentile, method="linear"))
            assert stats["quantiles"][key] == pytest.approx(expected, rel=1e-12)
