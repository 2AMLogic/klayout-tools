"""Guards on ``klt pex --resistance-network`` that must refuse rather than
emit a confidently wrong network (issue #6899, PR review): the device-terminal
mapping on co-located terminals, the numerical-zero conductance drop that can
disconnect a terminal, and the integrity ``open`` check on ``reduced`` nets.

Companion to ``test_extract_resistance_network.py`` (kept apart so neither
file outgrows the per-file size ratchet).
"""

from types import SimpleNamespace

import pytest

from helpers.resistance_graph_fixtures import make_sky130_trunk_stub_layout
from klayout_tools.extract_resistance_network import (
    map_device_terminals,
    reduce_terminal_network,
    reduced_network_plan,
)
from klayout_tools.parasitic_integrity import (
    GROUND_OWNER,
    IntegrityRecordBuilder,
    ParasiticNetworkRecord,
    check_parasitic_network,
)
from test_extract_resistance_network import _run_pex_trunk

# A net with three graph terminals along y = 1.8 (x = 1, 5, 9 um).
GRAPH = ["poly_contact@1.0,1.8", "cuts@5.0,1.8", "cuts@9.0,1.8"]


def test_map_device_terminals_refuses_co_located_terminals():
    """Two terminals of one device (a diode-connected MOS: gate and drain on
    the net) report the same device position, so geometry cannot say which
    graph terminal is which. The nearest-position match used to return an
    arbitrary permutation with no mismatch; the position is inside the graph
    span, so the span guard never fired either."""
    gate_and_drain = [(3.0, 1.5), (3.0, 1.5), (9.0, 1.5)]
    assert map_device_terminals(GRAPH, gate_and_drain) == (None, "terminal_mismatch")
    assert map_device_terminals(GRAPH, [(3.0, 1.5)] * 3) == (
        None,
        "terminal_mismatch",
    )
    # Which of the two co-located terminals comes first must not matter.
    assert map_device_terminals(GRAPH, gate_and_drain[::-1]) == (
        None,
        "terminal_mismatch",
    )


def test_map_device_terminals_refuses_a_geometric_tie_between_devices():
    """Distinct device positions, mirror-symmetric about two graph
    terminals: each device is equidistant from both, so only the index
    tie-break would decide. Not the co-located case -- a refusal on geometry
    alone."""
    graph = ["cuts@0.0,0.0", "cuts@4.0,0.0"]
    assert map_device_terminals(graph, [(2.0, 0.0), (2.0, 1.0)]) == (
        None,
        "terminal_mismatch",
    )
    # A tie that the other devices resolve is not ambiguous: the second
    # device sits exactly on `cuts@4.0` and claims it first.
    mapping, reason = map_device_terminals(graph, [(2.0, 0.0), (4.0, 0.0)])
    assert reason is None and mapping is not None
    assert mapping.graph_indices == (0, 1)


def test_map_device_terminals_still_maps_distinct_devices():
    """Control for the refusals above: distinct device positions, each
    clearly nearest its own graph terminal, in any input order."""
    mapping, reason = map_device_terminals(GRAPH, [(1.0, 1.5), (5.0, 1.5), (9.0, 1.5)])
    assert reason is None and mapping is not None
    assert mapping.graph_indices == (0, 1, 2)
    mapping, reason = map_device_terminals(GRAPH, [(9.0, 1.5), (1.0, 1.5), (5.0, 1.5)])
    assert reason is None and mapping is not None
    assert mapping.graph_indices == (2, 0, 1)


def _fake_terminal_ref(x: float, y: float) -> SimpleNamespace:
    """The one attribute chain ``_terminal_star_positions_um`` reads."""
    disp = SimpleNamespace(x=x, y=y)
    return SimpleNamespace(
        device=lambda: SimpleNamespace(trans=SimpleNamespace(disp=disp))
    )


def _plan_entry() -> dict:
    return {
        "resistance_graph": {
            "status": "ok",
            "resistance_source": "legacy",
            "resistance_source_reason": "terminal_count_not_two",
            "terminals": list(GRAPH),
            "pair_resistance_ohm": [[0.0, 4.0, 8.0], [4.0, 0.0, 4.0], [8.0, 4.0, 0.0]],
        }
    }


def _plan(entry: dict, positions: list[tuple[float, float]]):
    return reduced_network_plan(
        entry,
        [_fake_terminal_ref(x, y) for x, y in positions],
        emit_resistance_network=True,
        variant_dropped_r=False,
        variant_dropped_c=False,
        emit_selected_resistance=False,
    )


def test_plan_reports_terminal_mismatch_for_co_located_terminals():
    """End to end through the per-net plan: the net keeps its legacy network
    and the report says why, instead of ``written: reduced`` with a null
    fallback reason."""
    entry = _plan_entry()
    network, mapping = _plan(entry, [(3.0, 1.5), (3.0, 1.5), (9.0, 1.5)])
    assert network is None and mapping is None
    assert entry["resistance_graph"]["network"] == {
        "written": "legacy",
        "resistor_count": None,
        "fallback_reason": "terminal_mismatch",
    }

    control = _plan_entry()
    network, mapping = _plan(control, [(1.0, 1.5), (5.0, 1.5), (9.0, 1.5)])
    assert network is not None and mapping is not None
    assert control["resistance_graph"]["network"]["written"] == "reduced"


def test_reduction_refuses_a_terminal_with_no_surviving_resistor():
    """Three terminals, 0-1 joined by 1 ohm and terminal 2 hanging off node 0
    through 1e10 ohm: its only conductance is below 1e-9 of the largest, so
    the drop used to strip it of every resistor -- a DC-floating node in the
    netlist, pairs (0,2) and (1,2) unreproduced, reason ``None``."""
    pairs = [
        [0.0, 1.0, 1e10],
        [1.0, 0.0, 1e10 + 1.0],
        [1e10, 1e10 + 1.0, 0.0],
    ]
    assert reduce_terminal_network(pairs) == (None, "pair_resistance_unavailable")


def test_reduction_refuses_terminals_split_into_two_groups():
    """The same defect without an isolated terminal: every terminal keeps a
    resistor but the survivors form two components, so cross pairs are lost.
    Checking connectivity, not just membership, covers the whole class."""
    far = 1e12
    pairs = [
        [0.0, 1.0, far, far],
        [1.0, 0.0, far, far],
        [far, far, 0.0, 1.0],
        [far, far, 1.0, 0.0],
    ]
    assert reduce_terminal_network(pairs) == (None, "pair_resistance_unavailable")


def test_reduction_keeps_a_wide_but_connected_spread():
    """Control: a six-decade spread that the drop leaves connected (0-1 at
    1 ohm, 1-2 at 1e6 ohm) must still emit, so the refusals above are not
    just 'refuse anything wide'."""
    pairs = [
        [0.0, 1.0, 1e6 + 1.0],
        [1.0, 0.0, 1e6],
        [1e6 + 1.0, 1e6, 0.0],
    ]
    network, reason = reduce_terminal_network(pairs)
    assert reason is None and network is not None
    emitted = {
        (r.terminal_a, r.terminal_b): r.resistance_ohm for r in network.resistors
    }
    assert emitted[(0, 1)] == pytest.approx(1.0, rel=1e-6)
    assert emitted[(1, 2)] == pytest.approx(1e6, rel=1e-6)


def _reduced_record(*, isolate_last_terminal: bool) -> ParasiticNetworkRecord:
    """One 3-terminal ``reduced`` net (``nt``, net_id 1): pair resistors
    between per-terminal leg nodes, each with its share of the capacitance."""
    b = IntegrityRecordBuilder()
    ground = b.add_node("vsubs", GROUND_OWNER)
    legs = [b.add_node(name, 1) for name in ("nt", "nt__t1", "nt__t2")]
    b.add_resistor("nt_g0", legs[0], legs[1], 5.0, 1)
    if not isolate_last_terminal:
        b.add_resistor("nt_g1", legs[1], legs[2], 7.0, 1)
    for i, leg in enumerate(legs):
        b.add_ground_capacitor(f"nt_n{i}", leg, ground, 1.0, 1)
    b.add_net(1, "nt", "reduced", tuple(legs), 3.0)
    return b.build(ParasiticNetworkRecord())


def test_integrity_open_check_runs_on_reduced_nets():
    """A ``reduced`` net's terminals are joined only by the emitted pair
    resistors, so connectivity is exactly what is not true by construction:
    the real union-find runs (it used to report ``not_applicable`` with the
    lumped model's reason), passing a healthy net and naming a floating
    terminal on a broken one."""
    (healthy,) = check_parasitic_network(_reduced_record(isolate_last_terminal=False))[
        "nets"
    ]
    assert healthy["rc_model"] == "reduced"
    assert healthy["open"]["status"] == "pass"

    result = check_parasitic_network(_reduced_record(isolate_last_terminal=True))
    (broken,) = result["nets"]
    assert broken["open"]["status"] == "open"
    assert broken["open"]["disconnected_count"] == 1
    assert broken["open"]["disconnected_terminals"] == ["nt__t2"]
    assert result["open_nets"] == 1


def test_pex_reduced_trunk_net_passes_the_integrity_open_check(tmp_path):
    """The real emission: the trunk fixture's reduced ``NT`` net reports a
    passing ``open`` check, not ``not_applicable``."""
    report, _out = _run_pex_trunk(
        tmp_path,
        "trunk_open",
        make_sky130_trunk_stub_layout(),
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
        resistance_network=True,
    )
    nets = {n["net"]: n for n in report["extraction"]["parasitic_integrity"]["nets"]}
    assert nets["NT"]["rc_model"] == "reduced"
    opened = nets["NT"]["integrity"]["open"]
    assert opened["status"] == "pass"
    assert opened["disconnected_count"] == 0
