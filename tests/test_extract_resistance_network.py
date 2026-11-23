"""The reduced terminal network of ``klt pex --resistance-network``
(issue #6899): the Kron-reduction unit tests (hand-computed matrices, a
random-network property test against an independent nodal oracle), the
device-terminal mapping, the per-net classification, and the trunk-plus-
dead-stub layout fixture through the real ``run_pex`` emission.
"""

import json
import math
import random
from pathlib import Path

import pytest

from helpers.resistance_graph_fixtures import make_sky130_trunk_stub_layout
from klayout_tools.extract import ExtractError, run_extract
from klayout_tools.extract_resistance_network import (
    map_device_terminals,
    network_written_report,
    reduce_terminal_network,
)
from klayout_tools.pex import PexError, run_pex
from klayout_tools.pex_resistance_graph import _resistance_graph_echo
from test_pex import _write_gds

# ---------------------------------------------------------------------------
# Nodal oracle (independent of the module under test): effective resistance
# of a conductor list by Gaussian elimination on the grounded Laplacian.
# ---------------------------------------------------------------------------


def _nodal_pair_resistances(
    conductances: list[tuple[int, int, float]], n: int
) -> list[list[float]]:
    """Effective resistances of a conductor list by Gaussian elimination
    on the grounded Laplacian (node 0 is ground): R_0j = v_j[j] for the
    unit injection at j, R_ij = v_i[i] + v_j[j] - 2 v_i[j] otherwise."""
    laplacian = [[0.0] * n for _ in range(n)]
    for i, j, g in conductances:
        laplacian[i][i] += g
        laplacian[j][j] += g
        laplacian[i][j] -= g
        laplacian[j][i] -= g

    def inject(i: int) -> list[float]:
        m = n - 1
        a = [row[1:] for row in laplacian[1:]]
        x = [1.0 if k == i else 0.0 for k in range(1, n)]
        for col in range(m):
            piv = max(range(col, m), key=lambda r: abs(a[r][col]))
            a[col], a[piv] = a[piv], a[col]
            x[col], x[piv] = x[piv], x[col]
            pivot = a[col][col]
            a[col] = [v / pivot for v in a[col]]
            x[col] /= pivot
            for r in range(m):
                if r != col and a[r][col]:
                    factor = a[r][col]
                    a[r] = [v - factor * w for v, w in zip(a[r], a[col], strict=True)]
                    x[r] -= factor * x[col]
        return [0.0] + x

    injections = {i: inject(i) for i in range(1, n)}
    resistances = [[0.0] * n for _ in range(n)]
    for i in range(1, n):
        vi = injections[i]
        resistances[0][i] = resistances[i][0] = vi[i]
        for j in range(1, i):
            resistances[i][j] = resistances[j][i]
        for j in range(i, n):
            vj = injections[j]
            resistances[i][j] = vi[i] + vj[j] - 2 * vi[j]
    return resistances


def test_reduction_reproduces_hand_computed_networks():
    """A complete graph K3 whose pair resistance is 4/3 ohm everywhere (edge
    conductance 0.5 S) reduces back to three 2-ohm resistors; a 1/2/3 S star
    onto one hub reduces to the complete graph g_ij = c_i c_j / sum(c)."""
    network, reason = reduce_terminal_network(
        [[0.0, 4 / 3, 4 / 3], [4 / 3, 0.0, 4 / 3], [4 / 3, 4 / 3, 0.0]]
    )
    assert reason is None and network is not None
    assert [
        (r.terminal_a, r.terminal_b, r.resistance_ohm) for r in network.resistors
    ] == [
        (0, 1, pytest.approx(2.0)),
        (0, 2, pytest.approx(2.0)),
        (1, 2, pytest.approx(2.0)),
    ]

    conductances = [1.0, 2.0, 3.0]
    total = sum(conductances)
    star_pairs = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            if i != j:
                star_pairs[i][j] = 1 / conductances[i] + 1 / conductances[j]
    network, reason = reduce_terminal_network(star_pairs)
    assert reason is None and network is not None
    expected = sorted(
        total / (conductances[i] * conductances[j])
        for i in range(3)
        for j in range(i + 1, 3)
    )
    emitted = sorted(r.resistance_ohm for r in network.resistors)
    assert emitted == pytest.approx(expected)


def test_reduction_property_random_networks():
    """g_ij >= 0 and every pair of the reduced network reproduces the input
    matrix, on seeded random connected networks of up to 16 terminals with
    extra internal nodes the reduction must eliminate."""
    rng = random.Random(20261122)
    worst = 0.0
    for _trial in range(120):
        n = rng.randint(3, 16)
        total = n + rng.randint(0, 8)
        edges: list[tuple[int, int, float]] = [
            (i, rng.randrange(i), rng.uniform(0.05, 5.0)) for i in range(1, total)
        ]
        for _ in range(rng.randint(0, 2 * total)):
            i, j = rng.sample(range(total), 2)
            if not any(
                (a == i and b == j) or (a == j and b == i) for a, b, _g in edges
            ):
                edges.append((i, j, rng.uniform(0.05, 5.0)))
        full = _nodal_pair_resistances(edges, total)
        terminal_pairs = [[full[i][j] for j in range(n)] for i in range(n)]
        network, reason = reduce_terminal_network(terminal_pairs)
        assert network is not None, reason
        assert all(r.resistance_ohm > 0.0 for r in network.resistors)
        reduced = [
            (r.terminal_a, r.terminal_b, 1.0 / r.resistance_ohm)
            for r in network.resistors
        ]
        solved = _nodal_pair_resistances(reduced, n)
        for i in range(n):
            for j in range(n):
                worst = max(
                    worst,
                    abs(solved[i][j] - terminal_pairs[i][j])
                    / max(1e-12, abs(terminal_pairs[i][j])),
                )
    assert worst < 1e-9


def test_reduction_fallback_reasons():
    disconnected = [[0.0, None, 5.0], [None, 0.0, 4.0], [5.0, 4.0, 0.0]]
    assert reduce_terminal_network(disconnected) == (
        None,
        "pair_resistance_unavailable",
    )
    shorted = [[0.0, 0.0, 5.0], [0.0, 0.0, 5.0], [5.0, 5.0, 0.0]]
    assert reduce_terminal_network(shorted) == (None, "singular_system")
    assert reduce_terminal_network([[0.0]]) == (None, "pair_resistance_unavailable")


def test_map_device_terminals_matches_by_nearest_position():
    xs = (1.5, 10.5, 4.0, 6.0, 8.0)
    graph_terminals = [f"device@{x},1.8" for x in xs]
    device_positions = [(1.5, 1.5), (10.5, 1.5), (8.0, 1.5), (6.0, 1.5), (4.0, 1.5)]
    mapping, reason = map_device_terminals(graph_terminals, device_positions)
    assert reason is None and mapping is not None
    assert mapping.graph_indices == (0, 1, 4, 3, 2)
    assert map_device_terminals(graph_terminals, device_positions[:4])[1] == (
        "terminal_mismatch"
    )
    assert map_device_terminals(graph_terminals, [(1.5, 1.5)] * 4 + [(500.0, 1.5)]) == (
        None,
        "terminal_mismatch",
    )
    assert map_device_terminals(
        ["bare_name"] + graph_terminals[1:], device_positions
    ) == (None, "terminal_mismatch")


def test_network_written_report_classification():
    def report(graph, **kwargs):
        kwargs.setdefault("emit_selected_resistance", True)
        kwargs.setdefault("reduced", False)
        kwargs.setdefault("resistor_count", None)
        kwargs.setdefault("fallback_reason", None)
        return network_written_report(graph, **kwargs)

    assert report({"resistance_source": "legacy"}, reduced=True, resistor_count=10) == {
        "written": "reduced",
        "resistor_count": 10,
        "fallback_reason": None,
    }
    scalar = report({"resistance_source": "graph"})
    assert scalar["written"] == "scalar" and scalar["fallback_reason"] is None
    skipped = report(
        {
            "resistance_source": "legacy",
            "status": "skipped",
            "resistance_source_reason": "graph_status_skipped",
        }
    )
    assert skipped["written"] == "legacy"
    assert skipped["fallback_reason"] == "graph_status_skipped"
    unsupported = report(
        {
            "resistance_source": "legacy",
            "status": "unsupported",
            "resistance_source_reason": "graph_status_unsupported",
            "diagnostics": [{"code": "graph_too_large"}, {"code": "singular_system"}],
        }
    )
    assert unsupported["fallback_reason"] == "graph_too_large"
    assert report(
        {"resistance_source": "legacy"}, fallback_reason="mom_rlc_override"
    ) == {
        "written": "legacy",
        "resistor_count": None,
        "fallback_reason": "mom_rlc_override",
    }


def _run_pex_trunk(
    tmp_path: Path, layout_name: str, layout, **kwargs
) -> tuple[dict, Path]:
    layout_path = _write_gds(layout, tmp_path / f"{layout_name}.gds")
    out = tmp_path / f"{layout_name}.spice"
    report = run_pex(
        str(layout_path),
        [],
        "sky130",
        top="TOP",
        output=str(out),
        artifacts_dir=str(tmp_path / f"art_{layout_name}"),
        extract_only=True,
        **kwargs,
    )
    return report, out


def _trunk_network_cards(path: Path) -> dict[str, tuple[str, str, float]]:
    """Net NT's emitted pair resistors, by instance name."""
    cards: dict[str, tuple[str, str, float]] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("RNT_g"):
            cards[parts[0]] = (parts[1], parts[2], float(parts[3]))
    return cards


def _trunk_leg_positions(path: Path) -> dict[str, tuple[float, float]]:
    """Each NT leg net -> its device's placement, read from the netlist's
    own ``* device instance $k r0 *1 x,y`` comments -- an independent
    source for the leg <-> graph-terminal correspondence."""
    positions: dict[str, tuple[float, float]] = {}
    placement: tuple[float, float] | None = None
    for line in path.read_text().splitlines():
        if line.startswith("* device instance") and " r0 *1 " in line:
            tail = line.split(" r0 *1 ", 1)[1].split()[0]
            x_s, y_s = tail.split(",")
            placement = (float(x_s), float(y_s))
            continue
        parts = line.split()
        if (
            placement is not None
            and len(parts) >= 3
            and parts[0].startswith("R$")
            and parts[2].startswith("NT")
        ):
            positions.setdefault(parts[2], placement)
            placement = None
    return positions


def test_pex_resistance_network_trunk_fixture(tmp_path):
    """At the five-terminal cap, the written resistor cards reproduce every
    solved pair and the two live terminals see less than the legacy sum."""
    report, out = _run_pex_trunk(
        tmp_path,
        "trunk",
        make_sky130_trunk_stub_layout(),
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
        resistance_network=True,
        resistance_graph_caps={"max_terminals": 5},
    )
    echo = report["extraction"]["resistance_graph"]
    assert echo["resistance_network"] is True
    assert echo["caps"]["max_terminals"] == 5
    nets = {n["net"]: n for n in echo["nets"]}
    trunk = nets["NT"]
    assert trunk["network"] == {
        "written": "reduced",
        "resistor_count": 10,
        "fallback_reason": None,
    }
    for stub in ("R1", "R2", "S1", "S2", "S3"):
        assert nets[stub]["network"]["written"] == "legacy"
        assert nets[stub]["network"]["fallback_reason"] == "graph_status_skipped"

    # Leg -> graph-terminal index, from the netlist's device placements.
    terminal_names = trunk["terminals"]
    positions = [
        tuple(float(v) for v in name.rsplit("@", 1)[1].split(","))
        for name in terminal_names
    ]
    leg_terminal: dict[str, int] = {}
    for leg, (x, y) in _trunk_leg_positions(out).items():
        leg_terminal[leg] = min(
            range(len(terminal_names)),
            key=lambda k: math.hypot(positions[k][0] - x, positions[k][1] - y),
        )
    assert len(leg_terminal) == 5

    cards = _trunk_network_cards(out)
    assert len(cards) == 10
    assert all(value > 0.0 for _a, _b, value in cards.values())
    nodes = sorted({node for a, b, _v in cards.values() for node in (a, b)})
    index = {node: i for i, node in enumerate(nodes)}
    conductances = [(index[a], index[b], 1.0 / v) for _n, (a, b, v) in cards.items()]
    solved = _nodal_pair_resistances(conductances, len(nodes))
    pairs = trunk["pair_resistance_ohm"]
    for leg_a, i in leg_terminal.items():
        for leg_b, j in leg_terminal.items():
            if i == j:
                continue
            emitted = solved[index[leg_a]][index[leg_b]]
            assert emitted == pytest.approx(pairs[i][j], rel=1e-6), (leg_a, leg_b)

    # The live pair (R1 x=1.5, R2 x=10.5) beats the legacy series sum.
    r1 = terminal_names.index("device@1.5000,1.8000")
    r2 = terminal_names.index("device@10.5000,1.8000")
    leg_of = {i: leg for leg, i in leg_terminal.items()}
    live = solved[index[leg_of[r1]]][index[leg_of[r2]]]
    assert live == pytest.approx(pairs[r1][r2], rel=1e-6)
    assert live < trunk["legacy_resistance_ohm"]

    # The model disclosure names the mode (a signoff item-7 citation key).
    assert "resistance_network" in (report["extraction"]["model"] or {})


def test_pex_resistance_network_off_is_byte_identical(tmp_path):
    """Without the flag the written netlist and report carry nothing new."""
    layout = make_sky130_trunk_stub_layout()
    plain_report, plain_out = _run_pex_trunk(tmp_path, "plain", layout)
    graph_report, graph_out = _run_pex_trunk(
        tmp_path,
        "graph",
        layout,
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
    )
    assert plain_out.read_bytes() == graph_out.read_bytes()
    assert (
        plain_report["extraction"]["netlist_sha256"]
        == graph_report["extraction"]["netlist_sha256"]
    )
    assert "resistance_graph" not in plain_report["extraction"]
    for net in graph_report["extraction"]["resistance_graph"]["nets"]:
        assert "network" not in net
    assert "resistance_network" not in (graph_report["extraction"]["model"] or {})
    assert "resistance_network" not in graph_report["extraction"]["resistance_graph"]


def test_pex_resistance_network_terminal_mismatch_keeps_star(tmp_path):
    """A graph terminal with no device behind it fails the one-to-one
    mapping: the net keeps the star and reports ``terminal_mismatch``."""
    report, out = _run_pex_trunk(
        tmp_path,
        "mismatch",
        make_sky130_trunk_stub_layout(extra_unattached_terminal=True),
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
        resistance_network=True,
    )
    nets = {n["net"]: n for n in report["extraction"]["resistance_graph"]["nets"]}
    assert nets["NT"]["network"] == {
        "written": "legacy",
        "resistor_count": None,
        "fallback_reason": "terminal_mismatch",
    }
    assert not _trunk_network_cards(out)


def test_pex_resistance_network_mom_rlc_override_wins(tmp_path):
    """A ``--mom-rlc-resistance-ohm`` substitution keeps the star even in
    network mode -- the caller-supplied value must win, as it already does
    against ``emit_selected_resistance``."""
    report, out = _run_pex_trunk(
        tmp_path,
        "mom",
        make_sky130_trunk_stub_layout(),
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
        resistance_network=True,
        mom_rlc_net="NT",
        mom_rlc_resistance_ohm=123.0,
    )
    nets = {n["net"]: n for n in report["extraction"]["resistance_graph"]["nets"]}
    assert nets["NT"]["network"]["fallback_reason"] == "mom_rlc_override"
    assert not _trunk_network_cards(out)


def test_pex_resistance_network_flag_refusals(tmp_path):
    layout = make_sky130_trunk_stub_layout()
    with pytest.raises(PexError, match="--resistance-network requires"):
        _run_pex_trunk(tmp_path, "f1", layout, resistance_network=True)
    with pytest.raises(PexError, match="cannot be combined with --distributed-rc"):
        _run_pex_trunk(
            tmp_path,
            "f2",
            layout,
            resistance_graph=True,
            resistance_network=True,
            critical_nets=["NT"],
            distributed_rc=True,
        )


def test_pex_resistance_network_refused_under_runset_engine():
    from klayout_tools.pex_runset import refuse_unsupported

    marker = object()  # any non-None runset takes the refusal branch
    with pytest.raises(PexError, match="--resistance-network"):
        refuse_unsupported(
            marker, device_only_leg=False, tie_nets=None, resistance_network=True
        )
    refuse_unsupported(
        marker, device_only_leg=False, tie_nets=None, resistance_network=False
    )


def test_extract_resistance_network_refuses_spef(tmp_path):
    layout_path = _write_gds(make_sky130_trunk_stub_layout(), tmp_path / "trunk.gds")
    with pytest.raises(ExtractError, match="cannot be combined with --spef"):
        run_extract(
            str(layout_path),
            "sky130",
            top="TOP",
            parasitics=True,
            resistance_graph=True,
            emit_resistance_network=True,
            output=str(tmp_path / "x.spice"),
            spef_output=str(tmp_path / "x.spef"),
        )
    with pytest.raises(ExtractError, match="emit_resistance_network requires"):
        run_extract(
            str(layout_path),
            "sky130",
            top="TOP",
            parasitics=True,
            emit_resistance_network=True,
            output=str(tmp_path / "y.spice"),
        )


def test_resistance_graph_echo_carries_network_block():
    """The pex echo forwards the per-net ``network`` block verbatim and
    echoes the request flag."""
    parasitics = {
        # The echo derives the request echo from the extraction report's own
        # model disclosure (present exactly when the emission ran).
        "model": {"resistance_network": "opt-in"},
        "nets": [
            {
                "net": "NT",
                "net_id": 7,
                "resistance_graph": {
                    "resistance_source": "legacy",
                    "selected_resistance_ohm": 5114.5,
                    "legacy_resistance_ohm": 5114.5,
                    "status": "ok",
                    "terminals": ["a", "b", "c"],
                    "pair_resistance_ohm": [[0.0, 1.0, 2.0]],
                    "network": {
                        "written": "reduced",
                        "resistor_count": 3,
                        "fallback_reason": None,
                    },
                },
            }
        ],
    }
    echo = _resistance_graph_echo(parasitics, 0.0)
    assert echo["resistance_network"] is True
    assert echo["nets"][0]["network"]["written"] == "reduced"
    plain = _resistance_graph_echo({"nets": parasitics["nets"]}, None)
    assert "resistance_network" not in plain
    assert "network" not in plain["nets"][0]
    assert json.dumps(echo["nets"][0])  # JSON-clean


def test_network_resistor_count_bound():
    """The dense reduction stays within T(T-1)/2 resistors for the graph
    build's 64-terminal cap (the documented size bound)."""
    rng = random.Random(4)
    for n in (3, 8, 16):
        pairs = _nodal_pair_resistances(
            [(i, rng.randrange(i), rng.uniform(0.1, 4.0)) for i in range(1, n)], n
        )
        network, reason = reduce_terminal_network(pairs)
        assert network is not None, reason
        assert len(network.resistors) <= n * (n - 1) // 2
