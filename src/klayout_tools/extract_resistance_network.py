"""Terminal-equivalent reduced resistor networks from a solved resistance
graph (issue #6899; Stage 3 netlist step of
``docs/design/extract-fidelity-roadmap.md`` section 3.D3).

:func:`reduce_terminal_network` turns a net's terminal-pair effective
resistance matrix (:class:`~klayout_tools.extract_resistance_graph.
TerminalResistanceMatrix.pair_resistance_ohm`) into the list of
terminal-to-terminal resistors whose nodal network reproduces every pair
value: the Kron reduction of the geometry-derived graph onto its device
terminals. Mathematically, with ``R`` the ``T x T`` pair matrix and
``J = I - 11^T / T`` the centering matrix, the reduced Laplacian is
``L_red = pinv(-0.5 J R J)`` (the Klein-Randic identity ``pinv(L) = -0.5 J
R J`` inverted; ``pinv`` is involutive on symmetric PSD matrices), and one
resistor of ``1 / g_ij`` ohm between terminals ``i`` and ``j`` for every
off-diagonal conductance ``g_ij = -L_red[i][j]``. Because ``L_red`` is a
Schur complement of a Laplacian it is itself a Laplacian, so every
``g_ij >= 0`` and no negative resistor is emitted; the property test proves
it. A full reduction is dense -- ``T`` terminals give at most
``T (T-1) / 2`` resistors (2016 at the graph build's ``MAX_GRAPH_TERMINALS``
= 64 cap) -- and only numerically-zero conductances (``g < 1e-9 * max g``)
are dropped, preserving the "every pair reproduces the matrix" guarantee.

:func:`map_device_terminals` grounds the emission on real nets: a graph
terminal is a geometry-derived ``"<kind>@<x_um>,<y_um>"`` name (a cut box
center), while the netlist side holds device-terminal refs whose only
position proxy is the owning device's placement (``Device.trans.disp``,
micrometres -- the same coarseness the star/ladder resistance split already
relies on). Each device terminal is matched to its nearest graph terminal,
one-to-one; a count mismatch, or an assignment farther than the graph
terminals' own pairwise spread (a device nowhere near this net's geometry),
reports ``terminal_mismatch`` and the net keeps its legacy network.

:func:`network_written_report` classifies, per net, which network the
emitted netlist actually carried (``reduced`` / ``scalar`` / ``legacy``) and
why any fallback happened, reusing the ``resistance_source_reason``
vocabulary of issues #5747/#2983 and adding only ``terminal_mismatch``,
``mom_rlc_override`` and ``parasitic_variant``, which it cannot express.

Everything here is pure arithmetic on plain lists -- no klayout import, no
extract-module import -- so the module stays a leaf.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Conductances below this fraction of the largest are numerically zero
#: (terminal pairs the reduction left essentially disconnected) and are
#: dropped rather than emitted as ~1e12-ohm resistors.
_ZERO_CONDUCTANCE_REL = 1e-9

#: A computed negative conductance larger than this fraction of the largest
#: conductance means the input matrix was not a valid resistance matrix
#: (not conditionally negative semidefinite); the reduction reports
#: ``singular_system`` instead of emitting a negative resistor.
_NEGATIVE_CONDUCTANCE_REL = 1e-6


#: The additive ``parasitics.model.resistance_network`` disclosure text
#: (issue #6899), set on the report when the reduced-network emission is on
#: so an ``extraction.model`` citation (e.g. ``klt signoff --manifest``)
#: can tell a graph-derived network extraction from a series-sum one.
RESISTANCE_NETWORK_MODEL_NOTE = (
    "opt-in (klt pex --resistance-network): every net whose resistance "
    "graph solved ok with 3+ terminals and whose device terminals map "
    "one-to-one onto its graph terminals carries the Kron-reduced terminal "
    "network in place of the star (per net: resistance_graph.network); all "
    "other nets keep the model this block describes"
)


@dataclass(frozen=True)
class NetworkResistor:
    """One emitted terminal-to-terminal resistor (graph-terminal indices)."""

    terminal_a: int
    terminal_b: int
    resistance_ohm: float


@dataclass(frozen=True)
class ReducedNetwork:
    """The reduced terminal network of one solved pair matrix."""

    resistors: tuple[NetworkResistor, ...]
    dropped_conductances: int


@dataclass(frozen=True)
class TerminalMapping:
    """The device-terminal -> graph-terminal assignment of one net:
    ``graph_indices[i]`` is the graph terminal of device terminal ``i``."""

    graph_indices: tuple[int, ...]


def parse_graph_terminal_position(name: str) -> tuple[float, float] | None:
    """The ``(x_um, y_um)`` encoded in a ``"<kind>@<x>,<y>"`` terminal name,
    or ``None`` for a name that does not encode a position."""
    if "@" not in name:
        return None
    try:
        x_s, y_s = name.rsplit("@", 1)[1].split(",")
        return (float(x_s), float(y_s))
    except ValueError:
        return None


def _graph_span(positions: Sequence[tuple[float, float]]) -> float:
    """The largest pairwise distance among ``positions`` (0.0 for one)."""
    return max(
        (
            math.hypot(ax - bx, ay - by)
            for i, (ax, ay) in enumerate(positions)
            for bx, by in positions[i + 1 :]
        ),
        default=0.0,
    )


def map_device_terminals(
    graph_terminals: Sequence[str],
    device_positions: Sequence[tuple[float, float]],
) -> tuple[TerminalMapping | None, str | None]:
    """Match each device terminal to its nearest graph terminal one-to-one.

    Returns ``(mapping, None)`` on success or ``(None, "terminal_mismatch")``
    when the two sides disagree in count, a terminal name carries no
    position, or the greedy nearest assignment puts some device farther from
    its terminal than the graph terminals' own pairwise spread (a device
    whose placement is nowhere near this net). The assignment is the
    globally greedy one over distances sorted with index tie-breaks, so it
    is deterministic.
    """
    if not graph_terminals or len(graph_terminals) != len(device_positions):
        return None, "terminal_mismatch"
    positions = [parse_graph_terminal_position(name) for name in graph_terminals]
    if any(p is None for p in positions):
        return None, "terminal_mismatch"
    terminal_positions = [p for p in positions if p is not None]
    span = _graph_span(terminal_positions)
    candidates = sorted(
        (
            math.hypot(gx - dx, gy - dy),
            device_index,
            graph_index,
        )
        for device_index, (dx, dy) in enumerate(device_positions)
        for graph_index, (gx, gy) in enumerate(terminal_positions)
    )
    graph_of: dict[int, int] = {}
    taken: set[int] = set()
    for distance, device_index, graph_index in candidates:
        if device_index in graph_of or graph_index in taken:
            continue
        if distance > span:
            return None, "terminal_mismatch"
        graph_of[device_index] = graph_index
        taken.add(graph_index)
    if len(graph_of) != len(device_positions):
        return None, "terminal_mismatch"
    return (
        TerminalMapping(tuple(graph_of[i] for i in range(len(device_positions)))),
        None,
    )


def _invert_spd(matrix: list[list[float]]) -> list[list[float]] | None:
    """The inverse of a small symmetric positive-definite matrix by
    Gauss-Jordan with partial pivoting, or ``None`` when a pivot vanishes
    (numerically singular input -- a disconnected or degenerate terminal
    set). Pure Python: the terminal cap (64) keeps this tiny."""
    n = len(matrix)
    scale = max(abs(v) for row in matrix for v in row)
    work = [
        list(row) + [1.0 if i == j else 0.0 for j in range(n)]
        for i, row in enumerate(matrix)
    ]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(work[r][col]))
        if abs(work[pivot_row][col]) <= 1e-12 * scale:
            return None
        if pivot_row != col:
            work[col], work[pivot_row] = work[pivot_row], work[col]
        pivot = work[col][col]
        work[col] = [v / pivot for v in work[col]]
        for r in range(n):
            if r != col and work[r][col] != 0.0:
                factor = work[r][col]
                work[r] = [
                    v - factor * w for v, w in zip(work[r], work[col], strict=True)
                ]
    return [row[n:] for row in work]


def _pair_matrix_unavailable(
    pair_resistance_ohm: Sequence[Sequence[float | None]],
) -> bool:
    """Whether any off-diagonal pair is ``null`` or invalid -- terminals in
    more than one connected component, or a non-finite/negative entry the
    reduction cannot treat as a resistance."""
    return any(
        (i != j) and (value is None or not math.isfinite(value) or value < 0)
        for i, row in enumerate(pair_resistance_ohm)
        for j, value in enumerate(row)
    )


def _double_centered(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    """``J M J`` (row/column means and the grand mean removed) -- for
    ``M = -0.5 R`` this is ``-0.5 J R J``, for the padded grounded inverse
    it is the entrywise pseudoinverse of that matrix."""
    n = len(matrix)
    row_means = [sum(row) / n for row in matrix]
    grand_mean = sum(row_means) / n
    return [
        [matrix[i][j] - row_means[i] - row_means[j] + grand_mean for j in range(n)]
        for i in range(n)
    ]


def _reduced_laplacian(
    pair_resistance_ohm: Sequence[Sequence[float]],
) -> list[list[float]] | None:
    """The reduced Laplacian ``pinv(-0.5 J R J)`` of one validated pair
    matrix, or ``None`` when the centered matrix is not invertible on the
    terminal subspace (a degenerate matrix, e.g. duplicate terminals).

    ``pinv(C) = J G J`` with ``G`` the grounded inverse (``C[1:,1:]``
    inverted, padded with a zero reference row/column): ``G`` solves
    ``C x = w`` on ``span{1}^perp``, and the double centering strips the
    null-space component, making the result the entrywise pseudoinverse --
    a symmetric PSD matrix with null space ``span{1}``, whose off-diagonal
    conductances ``g_ij = -pinv(C)[i][j]`` define the emitted resistors.
    """
    centered = _double_centered(
        [[-0.5 * float(value) for value in row] for row in pair_resistance_ohm]
    )
    sub_inverse = _invert_spd([row[1:] for row in centered[1:]])
    if sub_inverse is None:
        return None
    n = len(centered)
    grounded = [[0.0] * n for _ in range(n)]
    for i in range(1, n):
        for j in range(1, n):
            grounded[i][j] = sub_inverse[i - 1][j - 1]
    return _double_centered(grounded)


def reduce_terminal_network(
    pair_resistance_ohm: Sequence[Sequence[float | None]],
) -> tuple[ReducedNetwork | None, str | None]:
    """The Kron-reduced terminal network of one solved pair matrix.

    Returns ``(network, None)`` or ``(None, reason)``:
    ``pair_resistance_unavailable`` when any off-diagonal pair is ``null``
    or invalid (terminals in more than one connected component) and
    ``singular_system`` when the centered matrix is not invertible on the
    terminal subspace (a degenerate matrix, e.g. duplicate terminals, or
    one that is not conditionally negative semidefinite and would demand a
    negative resistor). The returned resistors are in sorted ``(a, b)``
    index order, ``1 / g`` ohm each.
    """
    n = len(pair_resistance_ohm)
    if n < 2 or _pair_matrix_unavailable(pair_resistance_ohm):
        return None, "pair_resistance_unavailable"
    finite = [
        [float(value) if value is not None else 0.0 for value in row]
        for row in pair_resistance_ohm
    ]
    gamma = _reduced_laplacian(finite)
    if gamma is None:
        return None, "singular_system"
    conductances = [(i, j, -gamma[i][j]) for i in range(n) for j in range(i + 1, n)]
    largest = max(g for _i, _j, g in conductances)
    if largest <= 0.0:
        return None, "singular_system"
    return _resistors_from_conductances(conductances, largest)


def _resistors_from_conductances(
    conductances: list[tuple[int, int, float]], largest: float
) -> tuple[ReducedNetwork | None, str | None]:
    """The emit decision for one conductance list: one ``1 / g`` resistor
    per conductance above the numerical-zero threshold, ``singular_system``
    for a conductance negative enough to demand a negative resistor."""
    resistors: list[NetworkResistor] = []
    dropped = 0
    for i, j, g in conductances:
        if g < -_NEGATIVE_CONDUCTANCE_REL * largest:
            return None, "singular_system"
        if g < _ZERO_CONDUCTANCE_REL * largest:
            dropped += 1
            continue
        resistors.append(NetworkResistor(i, j, 1.0 / g))
    if not resistors:
        return None, "singular_system"
    return ReducedNetwork(tuple(resistors), dropped), None


def network_written_report(
    graph: Mapping[str, Any],
    *,
    emit_selected_resistance: bool,
    reduced: bool,
    resistor_count: int | None,
    fallback_reason: str | None,
) -> dict[str, Any]:
    """The additive ``resistance_graph.network`` block for one net (present
    only when the emission mode is on): which network the written netlist
    carried and, for anything but ``reduced``, why not.

    ``scalar`` is the two-terminal graph net whose
    ``selected_resistance_ohm`` issue #4037's ``emit_selected_resistance``
    writes; ``reduced`` is this issue's terminal network; every other net is
    ``legacy`` with a reason -- the emission-time token
    (``terminal_mismatch`` / ``singular_system`` / ``mom_rlc_override`` /
    ``parasitic_variant``) when the mode itself caused the fallback, else
    the net's established fallback vocabulary (first ``unsupported``
    diagnostic code, else ``resistance_source_reason``)."""
    written = "reduced" if reduced else "legacy"
    if not reduced and graph.get("resistance_source") == "graph":
        written = "scalar" if emit_selected_resistance else "legacy"
    if fallback_reason is None and written == "legacy":
        fallback_reason = _legacy_fallback_reason(graph)
    return {
        "written": written,
        "resistor_count": resistor_count if reduced else None,
        "fallback_reason": fallback_reason,
    }


def _legacy_fallback_reason(graph: Mapping[str, Any]) -> str | None:
    """Why a non-reduced net kept the legacy network, in the established
    vocabulary: the first diagnostic ``code`` for an ``unsupported`` graph
    (``klt extract`` sorts them by code, so this is deterministic), else the
    graph's own ``resistance_source_reason``."""
    if graph.get("status") == "unsupported":
        for diagnostic in graph.get("diagnostics") or []:
            code = diagnostic.get("code") if isinstance(diagnostic, dict) else None
            if code:
                return str(code)
    return graph.get("resistance_source_reason")


def reduced_network_report(
    graph_report: Any,
    network: ReducedNetwork | None,
    reason: str | None,
    *,
    emit_selected_resistance: bool,
) -> None:
    """Stamp the additive ``resistance_graph.network`` block (issue #6899)
    onto a net's graph report -- present only when the emission mode is on,
    so every other run's report is byte-identical."""
    graph_report["network"] = network_written_report(
        graph_report,
        emit_selected_resistance=emit_selected_resistance,
        reduced=network is not None,
        resistor_count=len(network.resistors) if network else None,
        fallback_reason=reason if network is None else None,
    )


def reduced_network_plan(
    entry: dict[str, Any],
    terminal_refs: list[Any],
    *,
    emit_resistance_network: bool,
    variant_dropped_r: bool,
    variant_dropped_c: bool,
    emit_selected_resistance: bool,
) -> tuple[ReducedNetwork | None, TerminalMapping | None]:
    """The per-net plan for ``--resistance-network`` (issue #6899):
    ``(network, mapping)``, plus the additive ``resistance_graph.network``
    report block stamped onto the net's graph report.

    ``network`` is non-``None`` only when the net's graph solved ``ok`` with
    three or more terminals (``terminal_count_not_two``), every pair value
    is finite, and every device terminal maps one-to-one onto a graph
    terminal -- the conditions under which :func:`_inject_parasitics` may
    replace the star with the Kron-reduced terminal network. A net that is
    not a candidate (mode off, no graph block, ``unsupported``/``skipped``
    graph, a two-terminal graph net, a ``--mom-rlc-resistance-ohm``
    override or a ``--parasitic-variant`` drop, which must win) reports
    ``legacy`` with the fallback reason its report already carries except
    where the mode itself caused it (``mom_rlc_override`` /
    ``parasitic_variant`` / ``terminal_mismatch`` / the reduction's own
    ``singular_system`` / ``pair_resistance_unavailable``), and keeps its
    star/Gamma-shunt path. Kept module-level, out of
    :func:`_inject_parasitics` (complexity-baselined), like
    :func:`_lumped_resistance_ohm` beside it.
    """
    if not emit_resistance_network:
        return None, None
    graph_report = entry.get("resistance_graph")
    from .extract_terminal_topology import _terminal_star_positions_um

    reason: str | None = None
    network: ReducedNetwork | None = None
    mapping: TerminalMapping | None = None
    if entry.get("mom_rlc_r_override"):
        reason = "mom_rlc_override"
    elif variant_dropped_r or variant_dropped_c:
        reason = "parasitic_variant"
    elif isinstance(graph_report, dict):
        if graph_report.get("status") == "ok" and terminal_refs:
            if graph_report.get("resistance_source_reason") == "terminal_count_not_two":
                mapping, reason = map_device_terminals(
                    graph_report["terminals"],
                    _terminal_star_positions_um(terminal_refs),
                )
                if mapping is not None:
                    network, reason = reduce_terminal_network(
                        graph_report["pair_resistance_ohm"]
                    )
            else:
                reason = None  # not a multi-terminal candidate: legacy below
        else:
            reason = None  # unsupported/skipped: legacy with its own reason
    if isinstance(graph_report, dict):
        reduced_network_report(
            graph_report,
            network,
            reason,
            emit_selected_resistance=emit_selected_resistance,
        )
    return network, mapping
