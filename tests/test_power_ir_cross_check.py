"""Cross-check `klt power`'s static IR-drop solve against an **independent,
external DC analyzer** -- ngspice (issue #845, Phase 1b of the power/IR-drop
+ EM signoff epic #712).

Epic #712 requires the solver be "cross-checked against an external IR
analyzer on one design". No commercial/proprietary IR analyzer (Redhawk,
Voltus, ...) is installable in this repository's open-source, CI-runnable
environment, and vendoring one is out of the question. **ngspice stands in as
the external oracle**, and it is a defensible one rather than a fig leaf:

- Static IR drop *is* a DC operating-point solve on a resistive network with
  independent current/voltage sources -- literally what `.op` computes. A
  commercial IR analyzer's differentiator is capacity, grid extraction and
  vectorless activity modelling, not different physics for this step.
- ngspice is a genuinely independent implementation: a mature C codebase
  (Berkeley SPICE3 lineage) with its own sparse LU (SPARSE 1.3 / KLU) and its
  own netlist parser -- no shared code, no shared formulation, and no shared
  authors with `klayout_tools/ir_solver.py`'s pure-Python preconditioned
  conjugate gradients.
- This repository already trusts ngspice as its SPICE engine for `klt sim`
  (see `sim.py`), so it is not a dependency introduced to make a test pass.

The comparison is run twice: on a synthetic **mesh** (a genuinely 2-D network
with several loads and two pads, where a formulation error would not cancel)
and end to end on the **real routed design** in the corpus
(`tests/corpus/place_and_route/gcd.gds.gz`, a real `sky130_fd_sc_hd` GCD macro
produced by real Yosys + real OpenROAD) -- the "on one design" half of the
criterion. Both node voltages and per-branch currents are compared.

Tests skip (never fail) when ngspice is not installed, matching
`tests/test_sim.py`'s own convention for engine-dependent tests.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools.ir_solver import solve_ir_drop
from klayout_tools.power import run_power

CORPUS_DIR = Path(__file__).parent / "corpus"
PLACE_AND_ROUTE_GDS = CORPUS_DIR / "place_and_route" / "gcd.gds.gz"

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

#: Absolute agreement required between the two solvers, in volts. ngspice is
#: asked for 12 significant digits (`set numdgt=12`); at a ~1 V rail that is a
#: ~1e-12 V print resolution, so 1e-9 V is ~1000x the printed quantisation and
#: still ~1e-6 of a typical millivolt-scale droop. Any real formulation
#: difference (a sign, a missing branch, a mis-scaled conductance) is orders of
#: magnitude larger than this.
VOLTAGE_ABSTOL_V = 1e-9

#: Same idea for branch currents, in amps, against milliamp-scale currents.
CURRENT_ABSTOL_A = 1e-12

_PRINT_RE = re.compile(r"^([\w@\[\]().]+)\s*=\s*([-+0-9.eE]+)\s*$")


def _spice_deck(blocks: list[dict]) -> str:
    """Build one ngspice deck from several independent networks.

    Each block is ``{"tag", "edges", "pads", "injections"}``; node names are
    prefixed with the block's tag so several islands can share one deck (they
    remain electrically independent -- each block's own ideal voltage source
    fixes its pad node, and each current source returns to the shared ideal
    ground, so no current crosses between blocks).
    """
    lines = ["* klt power IR-drop cross-check against ngspice"]
    probes: list[str] = []
    for block in blocks:
        tag = block["tag"]
        for i, edge in enumerate(block["edges"]):
            name = f"r{tag}_{i}"
            lines.append(
                f"{name} {tag}_{edge['from']} {tag}_{edge['to']} "
                f"{edge['resistance_ohm']!r}"
            )
            probes.append(f"@{name}[i]")
        for i, (node_id, voltage) in enumerate(block["pads"].items()):
            lines.append(f"v{tag}_{i} {tag}_{node_id} 0 dc {voltage!r}")
        for i, (node_id, current) in enumerate(block["injections"].items()):
            if current == 0:
                continue
            # ngspice's `I n+ n- value` pushes `value` amps *out of* n+ and
            # into n-: a positive value at (node, 0) is a load drawing current
            # off the grid, i.e. our negative injection.
            if current < 0:
                lines.append(f"i{tag}_{i} {tag}_{node_id} 0 dc {-current!r}")
            else:
                lines.append(f"i{tag}_{i} 0 {tag}_{node_id} dc {current!r}")
        probes.extend(f"v({tag}_{node_id})" for node_id in block["nodes"])

    lines.append(".control")
    lines.append("set numdgt=12")
    lines.append("op")
    lines.extend(f"print {probe}" for probe in probes)
    lines.append(".endc")
    lines.append(".end")
    return "\n".join(lines) + "\n"


def _run_ngspice(deck: str, tmp_path: Path) -> dict[str, float]:
    """Run ``ngspice -b`` on ``deck`` and return every printed quantity,
    keyed by the lower-cased name ngspice echoes back (``v(tag_n0)``,
    ``@rtag_0[i]``)."""
    path = tmp_path / "cross_check.sp"
    path.write_text(deck)
    proc = subprocess.run(
        ["ngspice", "-b", str(path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    values: dict[str, float] = {}
    for line in proc.stdout.splitlines():
        match = _PRINT_RE.match(line.strip())
        if match:
            values[match.group(1).lower()] = float(match.group(2))
    if not values:
        raise AssertionError(
            "ngspice printed no operating point:\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return values


def _compare(blocks: list[dict], results: list[dict], values: dict[str, float]):
    """Assert every solved node voltage and branch current agrees with
    ngspice's. Returns ``(max_voltage_delta, max_current_delta, node_count)``
    so a caller can also assert the comparison was not vacuous."""
    max_v = 0.0
    max_i = 0.0
    compared = 0
    for block, result in zip(blocks, results, strict=True):
        tag = block["tag"]
        for node_id in block["nodes"]:
            ours = result["voltages"][node_id]
            assert ours is not None, f"{tag}/{node_id} was not solved"
            theirs = values[f"v({tag}_{node_id})"]
            max_v = max(max_v, abs(ours - theirs))
            assert ours == pytest.approx(theirs, abs=VOLTAGE_ABSTOL_V), (
                f"node {tag}/{node_id}: klt {ours!r} V vs ngspice {theirs!r} V"
            )
            compared += 1
        for i, edge in enumerate(block["edges"]):
            ours_i = result["edge_currents"][edge["id"]]
            theirs_i = values[f"@r{tag}_{i}[i]"]
            assert ours_i == pytest.approx(theirs_i, abs=CURRENT_ABSTOL_A), (
                f"edge {tag}/{edge['id']}: klt {ours_i!r} A vs ngspice {theirs_i!r} A"
            )
            max_i = max(max_i, abs(ours_i - theirs_i))
    return max_v, max_i, compared


def _solve_blocks(blocks: list[dict]) -> list[dict]:
    return [
        solve_ir_drop(
            block["nodes"],
            block["edges"],
            pads=block["pads"],
            injections=block["injections"],
        )
        for block in blocks
    ]


# --- Cross-check 1: a synthetic mesh ----------------------------------------


def _mesh_block(size: int = 7) -> dict:
    """A `size` x `size` mesh of unequal resistors, fed at two corners, loaded
    at several interior nodes -- a genuinely 2-D network whose answer nobody
    can write down by hand, which is the point: it exercises the solver where
    a closed form cannot."""
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
                        "resistance_ohm": 0.5 + 0.1 * ((i * size + j) % 5),
                    }
                )
            if j + 1 < size:
                edges.append(
                    {
                        "id": f"v{i}_{j}",
                        "from": f"n{i}_{j}",
                        "to": f"n{i}_{j + 1}",
                        "resistance_ohm": 0.25 + 0.05 * ((i + 3 * j) % 7),
                    }
                )
    injections = {
        f"n{i}_{j}": -1e-3 * (1 + ((i * 3 + j) % 4))
        for i in range(1, size - 1)
        for j in range(1, size - 1)
    }
    return {
        "tag": "mesh",
        "nodes": nodes,
        "edges": edges,
        "pads": {"n0_0": 1.8, f"n{size - 1}_{size - 1}": 1.8},
        "injections": injections,
    }


@_SKIP_NO_NGSPICE
def test_mesh_matches_ngspice_dc_operating_point(tmp_path):
    blocks = [_mesh_block()]
    results = _solve_blocks(blocks)
    assert results[0]["components"][0]["solved"] is True

    values = _run_ngspice(_spice_deck(blocks), tmp_path)
    max_v, max_i, compared = _compare(blocks, results, values)

    assert compared == 49
    # The comparison must be non-vacuous: there is a real droop to disagree
    # about (hundreds of microvolts to millivolts), and the two solvers agree
    # to nine-plus decimal places anyway.
    droops = [1.8 - v for v in results[0]["voltages"].values()]
    assert max(droops) > 1e-3
    assert max_v < VOLTAGE_ABSTOL_V
    assert max_i < CURRENT_ABSTOL_A


@_SKIP_NO_NGSPICE
def test_ground_bounce_sign_matches_ngspice(tmp_path):
    """The ground-return half of the current model: instances *inject* into a
    ground net, so its nodes rise above the 0 V pad. A sign error here would
    still look self-consistent inside our own solver -- ngspice is what
    catches it."""
    block = {
        "tag": "gnd",
        "nodes": ["n0", "n1", "n2", "n3"],
        "edges": [
            {"id": "e0", "from": "n0", "to": "n1", "resistance_ohm": 0.8},
            {"id": "e1", "from": "n1", "to": "n2", "resistance_ohm": 1.3},
            {"id": "e2", "from": "n2", "to": "n3", "resistance_ohm": 2.1},
        ],
        "pads": {"n0": 0.0},
        "injections": {"n2": 3e-3, "n3": 5e-3},
    }
    results = _solve_blocks([block])
    values = _run_ngspice(_spice_deck([block]), tmp_path)
    max_v, max_i, compared = _compare([block], results, values)

    assert compared == 4
    assert results[0]["voltages"]["n3"] > results[0]["voltages"]["n0"]
    assert max_v < VOLTAGE_ABSTOL_V
    assert max_i < CURRENT_ABSTOL_A


# --- Cross-check 2: the real routed design ----------------------------------


def _gcd_spec(pads: list[dict] | None = None, instances: list[dict] | None = None):
    """The same sky130 met1/met2/via1 stackup `test_power.py`'s own real-
    fixture acceptance test uses, plus this phase's `pads`/`current_model`."""
    spec = {
        "power_nets": ["VPWR", "VGND"],
        "stackup": [
            {
                "name": "met1",
                "layer": "68/20",
                "label_layer": "68/5",
                "sheet_resistance_ohm_per_sq": 0.125,
            },
            {
                "name": "met2",
                "layer": "69/20",
                "label_layer": "69/5",
                "sheet_resistance_ohm_per_sq": 0.125,
            },
        ],
        "vias": [
            {
                "name": "via1",
                "layer": "68/44",
                "between": ["met1", "met2"],
                "resistance_ohm": 4.5,
            }
        ],
    }
    if pads is not None:
        spec["pads"] = pads
    if instances is not None:
        spec["current_model"] = {
            "supply_net": "VPWR",
            "ground_net": "VGND",
            "instances": instances,
        }
    return spec


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)
@_SKIP_NO_NGSPICE
def test_gcd_routed_design_matches_ngspice(tmp_path):
    """The acceptance criterion, on a real routed design: extract the GCD
    macro's power grid, feed every island from a pad on its own left-hand rail
    end, hang a 0.2 mA load on each rail's far end, solve -- then hand the
    *same* network to ngspice and require the two to agree node for node and
    branch for branch.

    This design has no PDN (`klt par` does not generate one -- see
    `power.py`), so its power nets are many short, un-strapped per-row rails.
    That is the real topology; the cross-check is over all of it (every
    island, every node, every branch), not a hand-picked corner of it.
    """
    # Pass 1: extract only, to learn where this fixture's rails actually are.
    probe_spec = tmp_path / "gcd.probe.json"
    probe_spec.write_text(json.dumps(_gcd_spec()))
    extracted = run_power(str(PLACE_AND_ROUTE_GDS), str(probe_spec))
    assert extracted["ir_drop_map"] is None

    pads: list[dict] = []
    instances: list[dict] = []
    for net_entry in extracted["networks"]:
        for island in net_entry["islands"]:
            first, last = island["nodes"][0], island["nodes"][-1]
            pads.append(
                {
                    "name": f"pad_{island['island_id']}",
                    "net": net_entry["net"],
                    "x_um": first["x_um"],
                    "y_um": first["y_um"],
                    "voltage_v": 1.8 if net_entry["net"] == "VPWR" else 0.0,
                }
            )
            if net_entry["net"] == "VPWR":
                instances.append(
                    {
                        "name": f"load_{island['island_id']}",
                        "x_um": last["x_um"],
                        "y_um": last["y_um"],
                        "current_a": 2e-4,
                    }
                )

    spec = tmp_path / "gcd.power.json"
    spec.write_text(json.dumps(_gcd_spec(pads, instances)))
    report = run_power(str(PLACE_AND_ROUTE_GDS), str(spec))

    ir_drop = report["ir_drop_map"]
    assert ir_drop is not None
    assert report["worst_case_droop_mv"] > 0

    # Rebuild the solved islands as independent SPICE blocks.
    blocks: list[dict] = []
    results: list[dict] = []
    by_net = {entry["net"]: entry for entry in report["networks"]}
    for net_entry in ir_drop["nets"]:
        extraction = by_net[net_entry["net"]]
        for index, island in enumerate(net_entry["islands"]):
            if not island["solved"] or island["unsolved_node_count"]:
                continue
            geometry = extraction["islands"][index]
            tag = f"b{len(blocks)}"
            # The exact network `klt power` solved, read back out of its own
            # report -- resistances from the extraction, pad voltages and
            # injected currents from the IR-drop map. Deliberately *not*
            # re-derived from the solved voltages, which would make the
            # comparison circular: ngspice is handed the same inputs and must
            # independently produce the same outputs.
            pads_for_island = {
                node["id"]: node["pad_voltage_v"]
                for node in island["nodes"]
                if node["pad_voltage_v"] is not None
            }
            injections = {
                node["id"]: node["injected_current_a"] for node in island["nodes"]
            }
            blocks.append(
                {
                    "tag": tag,
                    "nodes": [node["id"] for node in geometry["nodes"]],
                    "edges": geometry["edges"],
                    "pads": pads_for_island,
                    "injections": injections,
                }
            )
            results.append(
                solve_ir_drop(
                    [node["id"] for node in geometry["nodes"]],
                    geometry["edges"],
                    pads=pads_for_island,
                    injections=injections,
                )
            )

    # Re-pinned for #1443: this fixture was regenerated with #1442's
    # row-rail fix applied, which merges what used to be many small,
    # gap-fragmented per-row islands into one continuous island per row per
    # net -- 34 total islands / 68 total nodes today (was ~193 islands /
    # ~386 nodes pre-#1443; see
    # `tests/test_power.py::test_gcd_fixture_extracts_a_real_power_grid`'s
    # docstring for the full explanation). The thresholds below keep the
    # same "most of the fixture solves, comparison is not vacuous" intent
    # against the new, smaller-but-still-real island/node counts.
    assert len(blocks) >= 30, "expected most of the fixture's islands to solve"

    values = _run_ngspice(_spice_deck(blocks), tmp_path)
    max_v, max_i, compared = _compare(blocks, results, values)

    assert compared >= 60
    assert max_v < VOLTAGE_ABSTOL_V
    assert max_i < CURRENT_ABSTOL_A
    # And the cross-check is about a real, non-zero droop, not about a grid
    # sitting flat at its pad voltage.
    assert report["worst_case_droop_mv"] > 1e-3
