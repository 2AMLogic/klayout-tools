"""Validate the static IR-drop solver against **canonical resistive networks
with closed-form answers** (issue #845, Phase 1b of the power/IR-drop + EM
signoff epic #712).

This is the epic's own reality-grounding discipline, applied literally: epic
#712 requires the solver be "validated against the canonical closed-form
resistive networks ... before any real grid". Every expected value in this
file is written down from circuit theory -- a series ladder, a distributed
load, parallel paths, a Wheatstone bridge, the infinite-square-lattice
Green's function -- never captured from this solver's own output. If the
solver regresses, these fail against physics, not against a golden blob.

**Stated tolerance.** The solver is exact-in-exact-arithmetic (conjugate
gradients on an SPD Dirichlet Laplacian terminates in at most `n` steps), so
the hand-solvable networks below are asserted to a *relative* tolerance of
`1e-9` -- floating-point round-off, not model error. The two lattice tests
are the exception and say so: they compare a **finite** grid against the
**infinite**-lattice closed form, so their tolerance (0.5 % / 1 %) is
dominated by the finite-size truncation of the analytic result itself, not by
the solver.

The independent-implementation cross-check (ngspice's own DC operating point,
on both synthetic networks and a real routed design) lives in
`tests/test_power_ir_cross_check.py`.
"""

from __future__ import annotations

import math

import pytest

from klayout_tools.ir_solver import IRSolverError, solve_ir_drop, worst_deviation

#: Relative tolerance for the hand-solvable (finite, exact) networks. See the
#: module docstring: this is a round-off budget, not a model-error budget.
EXACT_RTOL = 1e-9


def _chain(n_segments: int, resistance_ohm: float = 1.0):
    """A 1-D ladder: `n_segments` resistors in series, nodes `n0..nN`."""
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


def _square_grid(size: int, resistance_ohm: float = 1.0):
    """A `size` x `size` uniform mesh of unit resistors -- the canonical
    "uniform grid with a point load" network epic #712 names."""
    nodes = [f"n{i}_{j}" for i in range(size) for j in range(size)]
    edges = []
    for i in range(size):
        for j in range(size):
            if i + 1 < size:
                edges.append(
                    {
                        "id": f"h{i}_{j}",
                        "from": f"n{i}_{j}",
                        "to": f"n{i + 1}_{j}",
                        "resistance_ohm": resistance_ohm,
                    }
                )
            if j + 1 < size:
                edges.append(
                    {
                        "id": f"v{i}_{j}",
                        "from": f"n{i}_{j}",
                        "to": f"n{i}_{j + 1}",
                        "resistance_ohm": resistance_ohm,
                    }
                )
    return nodes, edges


# --- Canonical network 1: a series ladder with a point load -----------------


def test_series_ladder_point_load_matches_ohms_law():
    """`N` unit resistors, supply at one end, a point load at the far end:
    the droop is exactly `I * N * R` (Ohm's law), and every node's droop is
    linear in its distance from the pad."""
    n = 100
    current_a = 1e-3
    nodes, edges = _chain(n, resistance_ohm=1.0)

    result = solve_ir_drop(
        nodes, edges, pads={"n0": 1.8}, injections={f"n{n}": -current_a}
    )

    component = result["components"][0]
    assert component["solved"] is True
    assert component["reason"] is None
    # Every electron the load draws comes out of the pad.
    assert component["pad_current_a"] == pytest.approx(current_a, rel=EXACT_RTOL)

    for k in range(n + 1):
        expected = 1.8 - current_a * k * 1.0
        assert result["voltages"][f"n{k}"] == pytest.approx(expected, rel=EXACT_RTOL)

    worst_id, worst = worst_deviation(result["deviations"])
    assert worst_id == f"n{n}"
    assert worst == pytest.approx(current_a * n, rel=EXACT_RTOL)

    # Series network: the same current flows in every branch.
    for i in range(n):
        assert result["edge_currents"][f"e{i}"] == pytest.approx(
            current_a, rel=EXACT_RTOL
        )


# --- Canonical network 2: a uniformly loaded rail ---------------------------


def test_uniformly_loaded_rail_matches_triangular_sum():
    """A rail of `N` unit segments fed from one end with an identical load on
    every node: segment `k` carries `(N - k + 1) * I`, so the far-end droop is
    the triangular number `I * R * N * (N + 1) / 2` -- the textbook
    distributed-load result, and the reason a real PDN is strapped from both
    ends."""
    n = 50
    current_a = 1e-4
    resistance_ohm = 0.5
    nodes, edges = _chain(n, resistance_ohm=resistance_ohm)

    result = solve_ir_drop(
        nodes,
        edges,
        pads={"n0": 1.8},
        injections={f"n{k}": -current_a for k in range(1, n + 1)},
    )

    expected_droop = current_a * resistance_ohm * n * (n + 1) / 2
    assert result["voltages"][f"n{n}"] == pytest.approx(
        1.8 - expected_droop, rel=EXACT_RTOL
    )
    _, worst = worst_deviation(result["deviations"])
    assert worst == pytest.approx(expected_droop, rel=EXACT_RTOL)

    # Segment k (between n{k} and n{k+1}) carries the current of every load
    # beyond it: (N - k) * I.
    for k in range(n):
        assert result["edge_currents"][f"e{k}"] == pytest.approx(
            (n - k) * current_a, rel=EXACT_RTOL
        )


def test_rail_fed_from_both_ends_droops_four_times_less():
    """The same uniformly loaded rail strapped at *both* ends -- the reason
    real PDNs are double-fed. Both cases have an exact discrete closed form
    (segment `k` carries the sum of the loads beyond it), and their ratio
    approaches the textbook continuous factor of 4:

    - single-ended, pad at `n0`, loads at `n1..n(N-1)`:
      `droop(m) = I*R*[m*(N-1) - m*(m-1)/2]`, worst at `m = N`;
    - double-ended, symmetric: each pad sources half the load, so
      `droop(m) = I*R*[m*(N-1)/2 - m*(m-1)/2]`, worst at the midpoint.
    """
    n = 40
    current_a = 1e-4
    nodes, edges = _chain(n, resistance_ohm=1.0)
    injections = {f"n{k}": -current_a for k in range(1, n)}

    one_end = solve_ir_drop(nodes, edges, pads={"n0": 1.0}, injections=injections)
    both_ends = solve_ir_drop(
        nodes, edges, pads={"n0": 1.0, f"n{n}": 1.0}, injections=injections
    )

    _, worst_one = worst_deviation(one_end["deviations"])
    worst_id, worst_both = worst_deviation(both_ends["deviations"])

    expected_one = current_a * (n * (n - 1) - n * (n - 1) / 2)
    mid = n // 2
    expected_both = current_a * (mid * (n - 1) / 2 - mid * (mid - 1) / 2)
    assert worst_one == pytest.approx(expected_one, rel=EXACT_RTOL)
    assert worst_both == pytest.approx(expected_both, rel=EXACT_RTOL)
    assert worst_id == f"n{mid}"
    # ... and that discrete ratio is the continuous factor of 4 to ~3 %.
    assert worst_one / worst_both == pytest.approx(4.0, rel=3e-2)

    # Both pads together still source the whole load current.
    assert both_ends["components"][0]["pad_current_a"] == pytest.approx(
        current_a * (n - 1), rel=EXACT_RTOL
    )


# --- Canonical network 3: series/parallel reduction -------------------------


def test_parallel_paths_reduce_to_the_parallel_resistance():
    """Two parallel branches (2 ohm and 3 ohm) between the pad and the load:
    droop = I * (2*3)/(2+3) = I * 1.2 ohm, and the branch currents divide
    inversely with resistance (current divider)."""
    nodes = ["pad", "load"]
    edges = [
        {"id": "a", "from": "pad", "to": "load", "resistance_ohm": 2.0},
        {"id": "b", "from": "pad", "to": "load", "resistance_ohm": 3.0},
    ]
    current_a = 0.01

    result = solve_ir_drop(
        nodes, edges, pads={"pad": 1.2}, injections={"load": -current_a}
    )

    r_parallel = (2.0 * 3.0) / (2.0 + 3.0)
    assert result["voltages"]["load"] == pytest.approx(
        1.2 - current_a * r_parallel, rel=EXACT_RTOL
    )
    assert result["edge_currents"]["a"] == pytest.approx(
        current_a * 3.0 / 5.0, rel=EXACT_RTOL
    )
    assert result["edge_currents"]["b"] == pytest.approx(
        current_a * 2.0 / 5.0, rel=EXACT_RTOL
    )


def test_balanced_wheatstone_bridge_carries_no_bridge_current():
    """A balanced bridge: the bridging resistor carries exactly zero current
    however large it is (R1/R2 == R3/R4). A solver that mis-signs an
    off-diagonal term cannot reproduce this."""
    nodes = ["top", "left", "right", "bottom"]
    edges = [
        {"id": "r1", "from": "top", "to": "left", "resistance_ohm": 10.0},
        {"id": "r2", "from": "left", "to": "bottom", "resistance_ohm": 20.0},
        {"id": "r3", "from": "top", "to": "right", "resistance_ohm": 30.0},
        {"id": "r4", "from": "right", "to": "bottom", "resistance_ohm": 60.0},
        {"id": "bridge", "from": "left", "to": "right", "resistance_ohm": 7.0},
    ]

    result = solve_ir_drop(
        nodes, edges, pads={"top": 5.0, "bottom": 0.0}, injections={}
    )

    assert result["voltages"]["left"] == pytest.approx(5.0 * 20 / 30, rel=EXACT_RTOL)
    assert result["voltages"]["right"] == pytest.approx(5.0 * 60 / 90, rel=EXACT_RTOL)
    assert result["edge_currents"]["bridge"] == pytest.approx(0.0, abs=1e-15)


# --- Canonical network 4: the infinite square lattice -----------------------


def test_uniform_grid_point_load_approaches_the_lattice_green_function():
    """The canonical "uniform grid with a point load": in an *infinite* square
    lattice of `R` resistors, the resistance between two adjacent nodes is
    exactly `R/2` (by symmetry + superposition -- the classic result). A large
    finite grid approaches it from above; 41x41 lands within 0.1 %.

    Tolerance here is set by the finite-grid truncation of the analytic
    infinite-lattice answer, not by the solver (see this module's docstring).
    """
    size = 41
    nodes, edges = _square_grid(size, resistance_ohm=1.0)
    centre = size // 2
    source = f"n{centre}_{centre}"
    sink = f"n{centre}_{centre + 1}"

    result = solve_ir_drop(nodes, edges, pads={sink: 0.0}, injections={source: 1.0})

    # V = I * R_effective with I = 1 A and the sink held at 0 V.
    r_effective = result["voltages"][source]
    assert r_effective == pytest.approx(0.5, rel=5e-3)
    assert r_effective > 0.5  # a finite grid is always stiffer than infinite

    component = result["components"][0]
    assert component["solved"] is True
    assert component["residual"] < 1e-9
    assert component["pad_current_a"] == pytest.approx(-1.0, rel=1e-9)


def test_uniform_grid_diagonal_load_approaches_two_over_pi():
    """The other closed-form lattice value: the resistance between two
    *diagonal* neighbours of an infinite square lattice is exactly `2R/pi`
    (Green's-function result), a number no finite-difference bug would
    reproduce by accident."""
    size = 41
    nodes, edges = _square_grid(size, resistance_ohm=1.0)
    centre = size // 2
    source = f"n{centre}_{centre}"
    sink = f"n{centre + 1}_{centre + 1}"

    result = solve_ir_drop(nodes, edges, pads={sink: 0.0}, injections={source: 1.0})

    assert result["voltages"][source] == pytest.approx(2.0 / math.pi, rel=1e-2)


# --- Modelling behaviours the map depends on --------------------------------


def test_island_without_a_pad_is_reported_unsolved_not_guessed():
    nodes, edges = _chain(3)
    result = solve_ir_drop(nodes, edges, pads={}, injections={"n3": -1e-3})

    component = result["components"][0]
    assert component["solved"] is False
    assert component["reason"] == "no_pad"
    assert component["injected_current_a"] == pytest.approx(-1e-3)
    assert all(v is None for v in result["voltages"].values())
    assert all(v is None for v in result["edge_currents"].values())
    assert worst_deviation(result["deviations"]) == (None, 0.0)


def test_disconnected_components_are_solved_independently():
    """Two disjoint sub-networks in one call: the one with a pad solves, the
    one without is reported unsolved -- exactly the un-strapped-island case a
    PDN-free routed design is full of."""
    nodes = ["a0", "a1", "b0", "b1"]
    edges = [
        {"id": "ea", "from": "a0", "to": "a1", "resistance_ohm": 2.0},
        {"id": "eb", "from": "b0", "to": "b1", "resistance_ohm": 2.0},
    ]

    result = solve_ir_drop(
        nodes, edges, pads={"a0": 1.0}, injections={"a1": -0.1, "b1": -0.1}
    )

    assert result["voltages"]["a1"] == pytest.approx(1.0 - 0.2, rel=EXACT_RTOL)
    assert result["voltages"]["b1"] is None
    assert result["node_component"]["a0"] != result["node_component"]["b0"]
    reasons = {c["reason"] for c in result["components"]}
    assert reasons == {None, "no_pad"}


def test_zero_resistance_edge_shorts_its_endpoints():
    """A `resistance_ohm: 0` edge (a via a caller chose not to model) is an
    ideal short: both endpoints sit at the same voltage, and the shorted
    branch's own current is reported as unknown rather than as a division by
    zero."""
    nodes = ["pad", "mid", "far"]
    edges = [
        {"id": "short", "from": "pad", "to": "mid", "resistance_ohm": 0.0},
        {"id": "wire", "from": "mid", "to": "far", "resistance_ohm": 4.0},
    ]

    result = solve_ir_drop(nodes, edges, pads={"pad": 1.0}, injections={"far": -0.01})

    assert result["voltages"]["mid"] == pytest.approx(1.0, rel=EXACT_RTOL)
    assert result["voltages"]["far"] == pytest.approx(1.0 - 0.04, rel=EXACT_RTOL)
    assert result["edge_currents"]["short"] is None
    assert result["edge_currents"]["wire"] == pytest.approx(0.01, rel=EXACT_RTOL)
    assert result["shorted_edge_ids"] == ["short"]


def test_pads_shorted_to_conflicting_voltages_are_not_silently_averaged():
    nodes = ["a", "b"]
    edges = [{"id": "short", "from": "a", "to": "b", "resistance_ohm": 0.0}]

    result = solve_ir_drop(nodes, edges, pads={"a": 1.8, "b": 0.0})

    component = result["components"][0]
    assert component["solved"] is False
    assert component["reason"] == "conflicting_pad_voltage"
    assert result["voltages"]["a"] is None


def test_no_current_means_every_node_sits_exactly_at_the_pad_voltage():
    nodes, edges = _chain(5)
    result = solve_ir_drop(nodes, edges, pads={"n0": 1.8}, injections={})

    assert all(v == pytest.approx(1.8) for v in result["voltages"].values())
    assert all(d == pytest.approx(0.0) for d in result["deviations"].values())
    assert result["components"][0]["iterations"] == 0


def test_current_drawn_at_the_pad_node_still_counts_against_the_pad():
    """A load whose nearest node *is* the pad's own node draws its current
    straight out of the pad with no droop. It still has to appear in
    `pad_current_a` -- conservation says the pads source every amp the model
    draws, including the amps that never traverse a resistor. (A real routed
    design hits this constantly: a short rail fed at one end has cells whose
    nearest node is the feed point itself.)"""
    nodes, edges = _chain(1, resistance_ohm=1.0)
    at_pad, downstream = 3e-3, 2e-3

    result = solve_ir_drop(
        nodes,
        edges,
        pads={"n0": 1.8},
        injections={"n0": -at_pad, "n1": -downstream},
    )

    component = result["components"][0]
    # Only the downstream load's current crosses the resistor, so only it
    # droops -- but the pad sources both loads.
    assert result["voltages"]["n1"] == pytest.approx(1.8 - downstream, rel=EXACT_RTOL)
    assert result["edge_currents"]["e0"] == pytest.approx(downstream, rel=EXACT_RTOL)
    assert component["pad_current_a"] == pytest.approx(
        at_pad + downstream, rel=EXACT_RTOL
    )
    assert component["pad_current_a"] == pytest.approx(
        -component["injected_current_a"], rel=EXACT_RTOL
    )


def test_ground_return_bounces_above_the_pad():
    """A ground net's instances *inject* current (positive), so its nodes sit
    **above** the 0 V pad -- ground bounce. `deviations` keeps the sign;
    `worst_deviation` reports the magnitude."""
    nodes, edges = _chain(4, resistance_ohm=0.25)
    result = solve_ir_drop(nodes, edges, pads={"n0": 0.0}, injections={"n4": 2e-3})

    assert result["voltages"]["n4"] == pytest.approx(2e-3 * 1.0, rel=EXACT_RTOL)
    assert result["deviations"]["n4"] > 0
    assert worst_deviation(result["deviations"]) == ("n4", pytest.approx(2e-3))


# --- Contract errors --------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"pads": {"nope": 1.0}}, "pad references unknown node"),
        ({"injections": {"nope": 1.0}}, "current injection references unknown node"),
    ],
)
def test_dangling_references_raise(kwargs, message):
    nodes, edges = _chain(2)
    with pytest.raises(IRSolverError, match=message):
        solve_ir_drop(nodes, edges, **kwargs)


def test_negative_resistance_raises():
    nodes = ["a", "b"]
    edges = [{"id": "e", "from": "a", "to": "b", "resistance_ohm": -1.0}]
    with pytest.raises(IRSolverError, match="negative/NaN resistance"):
        solve_ir_drop(nodes, edges, pads={"a": 1.0})


def test_duplicate_node_id_raises():
    with pytest.raises(IRSolverError, match="duplicate node id"):
        solve_ir_drop(["a", "a"], [])


def test_edge_referencing_an_unknown_node_raises():
    with pytest.raises(IRSolverError, match="references unknown node"):
        solve_ir_drop(
            ["a"], [{"id": "e", "from": "a", "to": "b", "resistance_ohm": 1.0}]
        )
