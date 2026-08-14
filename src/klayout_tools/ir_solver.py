"""Solve a resistive network for its DC operating point -- the static
IR-drop core behind ``klt power`` (issue #845, Phase 1b of the power/IR-drop
+ EM signoff epic #712).

Deliberately **geometry-free**: this module knows nothing about layouts,
layers, or microns. Its input is a graph -- node ids, resistor edges, pad
(fixed-voltage) nodes and current injections -- exactly the shape
``power.py``'s Phase 1a extraction already emits, and exactly the shape a
canonical closed-form resistive network can be written down in by hand. That
separation is what makes the epic's own reality-grounding discipline
testable: ``tests/test_ir_solver.py`` validates the solver against
hand-solvable ladders/meshes and the infinite-square-lattice Green's function
with no layout in sight, and ``tests/test_power_ir_cross_check.py``
cross-checks the same solver against ngspice's own DC operating point.

Method (stated plainly, because "the solver is right" is the whole claim):

- **Zero-resistance edges are merged, not divided by.** ``power.py``'s spec
  permits ``resistance_ohm: 0`` (an ideal short -- e.g. a via a caller does
  not want to model). Those edges union their endpoints into one *supernode*
  via union-find rather than contributing an infinite conductance. A shorted
  edge's own branch current is not recoverable from node voltages, so it is
  reported as ``None``.
- **Modified nodal analysis, Dirichlet form.** For every node ``u`` that is
  not held at a pad voltage, KCL is ``sum_v g_uv (V_u - V_v) = I_u``, where
  ``I_u`` is the current *injected into* ``u`` (an instance drawing current
  off a supply net injects a negative current; the same instance returning
  it to a ground net injects a positive one). Pad nodes are eliminated into
  the right-hand side rather than solved for, which leaves a symmetric
  positive-definite system whenever the component touches at least one pad.
- **Solved as a deviation from the pad reference, not as an absolute
  voltage.** The unknown is ``w = V - V_ref`` (``V_ref`` = the largest pad
  voltage in the component), so the iterative solver's convergence tolerance
  is relative to the *droop* rather than to the supply rail -- a 1 mV droop
  on a 1.8 V rail is resolved to the solver's full relative precision instead
  of being swamped by the rail it rides on.
- **Jacobi-preconditioned conjugate gradients.** The matrix is a weighted
  graph Laplacian with a Dirichlet boundary: symmetric positive definite, so
  CG applies and (in exact arithmetic) terminates in at most ``n`` steps. No
  numpy/scipy: ``klayout-tools``'s runtime dependency set is deliberately
  just ``klayout``/``jsonschema``, and a power grid is sparse enough that
  pure-Python CG over adjacency lists is the honest trade. The achieved
  relative residual is reported per component, never hidden.
- **A component with no pad is reported unsolved, never guessed.** An
  un-strapped power island has no DC path to a supply and genuinely has no
  operating point; ``power.py`` turns that into a warning naming the island
  (see that module's docstring on why a routed design without a PDN is
  mostly such islands).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Relative residual the conjugate-gradient iteration aims for.
DEFAULT_TOLERANCE = 1e-12

#: A component whose achieved relative residual is worse than this is
#: reported as ``solved: False`` / ``reason: "not_converged"`` rather than
#: returning numbers nobody should trust. It is deliberately far looser than
#: :data:`DEFAULT_TOLERANCE`: CG stalling a couple of digits short of 1e-12
#: on an ill-conditioned grid is normal floating-point behaviour, not a
#: failed solve, and the achieved residual is reported either way.
UNSOLVED_RESIDUAL = 1e-6

#: Two pads on the same (super)node whose voltages differ by more than this
#: are a contradiction -- a short between two different supplies -- and the
#: component is reported unsolved rather than silently picking one.
PAD_VOLTAGE_EPSILON = 1e-12


class IRSolverError(Exception):
    """Raised when the network handed to :func:`solve_ir_drop` is not a
    well-formed resistive network: a duplicate node id, an edge/pad/injection
    naming a node that does not exist, or a negative resistance.

    This is a *programming/contract* error in the caller's network, not a
    modelling outcome -- a network that is merely unsolvable (an island with
    no pad) is reported per component in the result, not raised.
    """


def _find(parent: list[int], x: int) -> int:
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:
        parent[x], x = root, parent[x]
    return root


def _union(parent: list[int], a: int, b: int) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[rb] = ra


def _pcg(
    diag: list[float],
    neighbours: list[list[tuple[int, float]]],
    b: list[float],
    tolerance: float,
    max_iterations: int,
) -> tuple[list[float], int, float]:
    """Jacobi-preconditioned conjugate gradients on the reduced (Dirichlet)
    Laplacian. Returns ``(solution, iterations, relative_residual)`` where the
    residual is recomputed exactly at the end rather than carried from the
    iteration's own recurrence (which drifts)."""
    n = len(b)
    x = [0.0] * n
    if n == 0:
        return x, 0, 0.0

    b_norm = math.sqrt(sum(v * v for v in b))
    if b_norm == 0.0:
        # Every injection is zero and every pad sits at the reference
        # voltage: the network is uniformly at the reference, exactly.
        return x, 0, 0.0

    def matvec(vec: list[float]) -> list[float]:
        out = [diag[i] * vec[i] for i in range(n)]
        for i, row in enumerate(neighbours):
            acc = 0.0
            for j, g in row:
                acc += g * vec[j]
            out[i] -= acc
        return out

    m_inv = [(1.0 / d if d > 0.0 else 1.0) for d in diag]
    r = list(b)
    z = [m_inv[i] * r[i] for i in range(n)]
    p = list(z)
    rz = sum(r[i] * z[i] for i in range(n))
    iterations = 0

    for _ in range(max_iterations):
        if math.sqrt(sum(v * v for v in r)) <= tolerance * b_norm:
            break
        ap = matvec(p)
        pap = sum(p[i] * ap[i] for i in range(n))
        if pap <= 0.0:
            # Not possible for an SPD system in exact arithmetic; bail out
            # rather than dividing by a non-positive curvature.
            break
        alpha = rz / pap
        for i in range(n):
            x[i] += alpha * p[i]
            r[i] -= alpha * ap[i]
        iterations += 1
        z = [m_inv[i] * r[i] for i in range(n)]
        rz_next = sum(r[i] * z[i] for i in range(n))
        if rz == 0.0:
            break
        beta = rz_next / rz
        rz = rz_next
        p = [z[i] + beta * p[i] for i in range(n)]

    true_r = [b[i] - v for i, v in enumerate(matvec(x))]
    residual = math.sqrt(sum(v * v for v in true_r)) / b_norm
    return x, iterations, residual


def solve_ir_drop(
    node_ids: Iterable[str],
    edges: Iterable[Mapping[str, Any]],
    *,
    pads: Mapping[str, float] | None = None,
    injections: Mapping[str, float] | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """Solve one resistive network for its DC node voltages.

    ``node_ids`` is every node in the network. ``edges`` is an iterable of
    mappings with ``id``/``from``/``to``/``resistance_ohm`` -- exactly the
    edge shape ``klt power``'s extraction emits (``docs/cli/power.md``).
    ``pads`` maps a node id to the voltage it is held at; ``injections`` maps
    a node id to the current (amps) injected *into* that node (negative for a
    load drawing current off the net).

    Returns a dict:

    - ``voltages``: ``{node_id: volts | None}`` -- ``None`` for a node in a
      component that could not be solved.
    - ``deviations``: ``{node_id: volts | None}`` -- each node's voltage
      minus its component's ``reference_voltage_v``. Negative on a supply
      net (droop), positive on a ground net (bounce); ``abs()`` of it is the
      IR drop.
    - ``edge_currents``: ``{edge_id: amps | None}`` -- signed ``from -> to``
      branch current, ``None`` for an unsolved component or a
      zero-resistance (shorted) edge.
    - ``components``: one entry per electrically connected component, in
      first-seen node order -- see the keys built below.
    - ``node_component``: ``{node_id: component_index}``.

    Raises :class:`IRSolverError` for a malformed network (duplicate node
    ids, dangling edge/pad/injection references, negative resistance).
    """
    pads = dict(pads or {})
    injections = dict(injections or {})

    node_list = list(node_ids)
    index: dict[str, int] = {}
    for node_id in node_list:
        if node_id in index:
            raise IRSolverError(f"duplicate node id {node_id!r}")
        index[node_id] = len(index)
    for node_id in pads:
        if node_id not in index:
            raise IRSolverError(f"pad references unknown node {node_id!r}")
    for node_id in injections:
        if node_id not in index:
            raise IRSolverError(
                f"current injection references unknown node {node_id!r}"
            )

    parent = list(range(len(node_list)))
    resistors: list[tuple[str, int, int, float]] = []
    shorted: list[str] = []
    for edge in edges:
        edge_id = str(edge["id"])
        for key in ("from", "to"):
            if edge[key] not in index:
                raise IRSolverError(
                    f"edge {edge_id!r} references unknown node {edge[key]!r}"
                )
        resistance = float(edge["resistance_ohm"])
        if resistance < 0 or math.isnan(resistance):
            raise IRSolverError(
                f"edge {edge_id!r} has a negative/NaN resistance ({resistance!r})"
            )
        a, b = index[edge["from"]], index[edge["to"]]
        if resistance == 0.0:
            _union(parent, a, b)
            shorted.append(edge_id)
        resistors.append((edge_id, a, b, resistance))

    # --- collapse to supernodes (zero-resistance edges merged) ------------
    super_of: list[int] = []
    super_index: dict[int, int] = {}
    for i in range(len(node_list)):
        root = _find(parent, i)
        if root not in super_index:
            super_index[root] = len(super_index)
        super_of.append(super_index[root])
    super_count = len(super_index)

    adjacency: list[dict[int, float]] = [{} for _ in range(super_count)]
    for _edge_id, a, b, resistance in resistors:
        if resistance == 0.0:
            continue
        sa, sb = super_of[a], super_of[b]
        if sa == sb:
            # A self-loop (or a loop closed by a short): carries no current
            # in a nodal formulation and contributes nothing to the matrix.
            continue
        g = 1.0 / resistance
        adjacency[sa][sb] = adjacency[sa].get(sb, 0.0) + g
        adjacency[sb][sa] = adjacency[sb].get(sa, 0.0) + g

    super_injection = [0.0] * super_count
    for node_id, current in injections.items():
        super_injection[super_of[index[node_id]]] += float(current)
    super_pads: list[list[float]] = [[] for _ in range(super_count)]
    for node_id, voltage in pads.items():
        super_pads[super_of[index[node_id]]].append(float(voltage))

    # --- connected components over the supernode graph --------------------
    component_of = [-1] * super_count
    components: list[list[int]] = []
    for start in range(super_count):
        if component_of[start] != -1:
            continue
        comp_index = len(components)
        stack = [start]
        component_of[start] = comp_index
        members = []
        while stack:
            u = stack.pop()
            members.append(u)
            for v in adjacency[u]:
                if component_of[v] == -1:
                    component_of[v] = comp_index
                    stack.append(v)
        components.append(sorted(members))

    super_voltage: list[float | None] = [None] * super_count
    super_reference: list[float | None] = [None] * super_count
    component_reports: list[dict[str, Any]] = []

    for comp_index, members in enumerate(components):
        member_set = set(members)
        pad_supers = [u for u in members if super_pads[u]]
        injected = sum(super_injection[u] for u in members)
        report: dict[str, Any] = {
            "index": comp_index,
            "solved": False,
            "reason": None,
            "reference_voltage_v": None,
            "pad_count": sum(len(super_pads[u]) for u in members),
            "injected_current_a": injected,
            "pad_current_a": None,
            "unknown_count": 0,
            "iterations": 0,
            "residual": 0.0,
        }

        conflicted = any(
            max(super_pads[u]) - min(super_pads[u]) > PAD_VOLTAGE_EPSILON
            for u in pad_supers
        )
        if not pad_supers:
            report["reason"] = "no_pad"
            component_reports.append(report)
            continue
        if conflicted:
            report["reason"] = "conflicting_pad_voltage"
            component_reports.append(report)
            continue

        reference = max(max(super_pads[u]) for u in pad_supers)
        pinned = {u: max(super_pads[u]) for u in pad_supers}
        unknowns = [u for u in members if u not in pinned]
        local = {u: i for i, u in enumerate(unknowns)}
        report["unknown_count"] = len(unknowns)

        diag = [0.0] * len(unknowns)
        neighbours: list[list[tuple[int, float]]] = [[] for _ in unknowns]
        rhs = [0.0] * len(unknowns)
        for u in unknowns:
            i = local[u]
            rhs[i] = super_injection[u]
            for v, g in adjacency[u].items():
                diag[i] += g
                if v in pinned:
                    rhs[i] += g * (pinned[v] - reference)
                else:
                    neighbours[i].append((local[v], g))

        iters = max_iterations
        if iters is None:
            iters = max(500, 10 * len(unknowns))
        solution, iterations, residual = _pcg(diag, neighbours, rhs, tolerance, iters)

        report["iterations"] = iterations
        report["residual"] = residual
        if residual > UNSOLVED_RESIDUAL:
            report["reason"] = "not_converged"
            component_reports.append(report)
            continue

        report["solved"] = True
        report["reference_voltage_v"] = reference
        for u in unknowns:
            super_voltage[u] = reference + solution[local[u]]
        for u, voltage in pinned.items():
            super_voltage[u] = voltage
        for u in member_set:
            super_reference[u] = reference

        # What the pads together deliver into this component. KCL at a pinned
        # node ``u`` reads ``P_u + I_u = sum_v g_uv (V_u - V_v)``: the pad
        # sources ``P_u`` and the current model injects ``I_u``, and together
        # they supply what leaves ``u`` through the resistors. So the pad's
        # own contribution is the resistive outflow *minus* any injection
        # attached to that same node -- dropping that second term silently
        # loses every load whose nearest node happens to be the pad's own
        # (common on a short rail fed at one end), which is exactly the
        # conservation law `power.py` reports per island.
        pad_current = 0.0
        for u in pinned:
            v_u = super_voltage[u]
            assert v_u is not None
            pad_current -= super_injection[u]
            for v, g in adjacency[u].items():
                v_v = super_voltage[v]
                assert v_v is not None
                pad_current += g * (v_u - v_v)
        report["pad_current_a"] = pad_current
        component_reports.append(report)

    voltages: dict[str, float | None] = {}
    deviations: dict[str, float | None] = {}
    node_component: dict[str, int] = {}
    for node_id in node_list:
        u = super_of[index[node_id]]
        node_component[node_id] = component_of[u]
        voltage = super_voltage[u]
        voltages[node_id] = voltage
        reference = super_reference[u]
        deviations[node_id] = (
            None if voltage is None or reference is None else voltage - reference
        )

    shorted_set = set(shorted)
    edge_currents: dict[str, float | None] = {}
    for edge_id, a, b, resistance in resistors:
        if edge_id in shorted_set:
            edge_currents[edge_id] = None
            continue
        v_a = super_voltage[super_of[a]]
        v_b = super_voltage[super_of[b]]
        edge_currents[edge_id] = (
            None if v_a is None or v_b is None else (v_a - v_b) / resistance
        )

    component_nodes: list[list[str]] = [[] for _ in components]
    for node_id in node_list:
        component_nodes[node_component[node_id]].append(node_id)
    for comp_index, member_nodes in enumerate(component_nodes):
        component_reports[comp_index]["node_ids"] = member_nodes
        component_reports[comp_index]["node_count"] = len(member_nodes)

    return {
        "voltages": voltages,
        "deviations": deviations,
        "edge_currents": edge_currents,
        "components": component_reports,
        "node_component": node_component,
        "shorted_edge_ids": shorted,
    }


def worst_deviation(
    deviations: Mapping[str, float | None], node_ids: Sequence[str] | None = None
) -> tuple[str | None, float]:
    """The ``(node_id, |deviation|)`` pair with the largest magnitude among
    ``node_ids`` (default: every solved node), or ``(None, 0.0)`` when no
    node was solved. Ties resolve to the first node in iteration order, so
    the answer is deterministic for a given network."""
    worst_id: str | None = None
    worst = 0.0
    for node_id in node_ids if node_ids is not None else deviations:
        value = deviations.get(node_id)
        if value is None:
            continue
        if worst_id is None or abs(value) > worst:
            worst_id = node_id
            worst = abs(value)
    return worst_id, worst
