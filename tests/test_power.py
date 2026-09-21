"""Tests for `klt power` and the `run_power` library function (issue #844,
Phase 1a of the power/IR-drop + EM signoff epic #712).

Most fixtures are generated programmatically with `klayout.db` inside the
tests, mirroring `tests/test_mom.py`'s convention. The acceptance test at
the bottom additionally runs against `tests/corpus/place_and_route/gcd.gds.gz`
-- a real `sky130_fd_sc_hd` GCD macro produced end to end by `klt synthesize`
+ `klt place-and-route` (real Yosys + real OpenROAD), the same corpus fixture
`test_drc.py`/`test_extract.py`/`test_lvs.py` already validate against (see
`tests/corpus/README.md`'s "Machine-generated macro-scale fixture" section)
-- satisfying this issue's "validated on at least one routed design produced
via klt par" acceptance criterion.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.power import PowerError, run_power

CORPUS_DIR = Path(__file__).parent / "corpus"
#: The `gcd` macro routed **without** `request.power` -- deliberately kept
#: (issue #2079) as this file's *negative* control: with no PDN, each
#: standard-cell row's rail is its own disconnected island (17 per net), the
#: exact topology `docs/cli/power.md`'s worked example documents and
#: `test_gcd_fixture_extracts_a_real_power_grid` below pins.
PLACE_AND_ROUTE_GDS = CORPUS_DIR / "place_and_route" / "gcd.gds.gz"
#: The same `gcd` design routed **with** a real `request.power` PDN (issue
#: #2079) -- this file's *positive* control, and the paired fixture that
#: makes the island counts above readable as a property of the request
#: rather than of `klt power`. See `tests/corpus/README.md`'s "`gcd-pdn`"
#: section for full provenance.
PLACE_AND_ROUTE_PDN_GDS = CORPUS_DIR / "place_and_route" / "gcd-pdn.gds.gz"
#: A real, fleet-canary routed digital block -- `sky130-modexp`'s own
#: `layout/modexp.gds` (issue #1322, Phase 2 of epic #712's acceptance
#: criterion 3: "validated ... on a real routed canary", not only the `gcd`
#: corpus fixture above). Vendored verbatim (Apache-2.0, see
#: `tests/corpus/README.md`'s "`modexp_canary.gds.gz`" section for full
#: provenance) from the public `2AMLogic/sky130-modexp` repo, produced by a
#: real `klt synthesize` + `klt place-and-route` (OpenROAD) run, `seed: 42`,
#: `sky130_fd_sc_hd`/`tt_025C_1v80`, 718 cells -- an order of magnitude
#: larger than `gcd`'s 69.
MODEXP_CANARY_GDS = CORPUS_DIR / "place_and_route" / "modexp_canary.gds.gz"


def _um(v: float) -> int:
    return int(round(v / 0.001))


def _basic_fixture(path) -> None:
    """Three electrically distinct power-net islands on a two-metal,
    one-via stackup:

    - `VPWR` island "A": an isolated met1 rail, x:[0,10] y:[0,1] um,
      labelled "VPWR" on met1's own label layer -- no via, no met2.
    - `VPWR` island "B": a met1 rail x:[0,10] y:[5,6] um, labelled "VPWR",
      bridged by a via (x:[4,6] y:[5,6] um) to a met2 stub x:[4,6] y:[5,10]
      um -- met2 carries no label of its own; its net name is inherited
      purely through via connectivity, exercising the "at least one
      stackup entry needs a label_layer" contract (not "every layer
      needs one").
    - `VGND` island "C": an isolated met1 rail x:[20,10] y:[0,1] um (i.e.
      x:[20,30]), labelled "VGND".
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")

    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    met2 = layout.layer(2, 0)
    via1 = layout.layer(3, 0)

    # Island A -- isolated met1 rail.
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(10), _um(1)))
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(5), _um(0.5))))

    # Island B -- met1 rail + via + met2 stub.
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(5), _um(10), _um(6)))
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(5), _um(5.5))))
    top.shapes(via1).insert(kdb.Box.new(_um(4), _um(5), _um(6), _um(6)))
    top.shapes(met2).insert(kdb.Box.new(_um(4), _um(5), _um(6), _um(10)))

    # Island C -- isolated met1 rail, different net.
    top.shapes(met1).insert(kdb.Box.new(_um(20), _um(0), _um(30), _um(1)))
    top.shapes(met1_label).insert(kdb.Text("VGND", kdb.Trans(_um(25), _um(0.5))))

    layout.write(str(path))


def _basic_spec(path, *, power_nets=("VPWR", "VGND")) -> None:
    path.write_text(
        json.dumps(
            {
                "power_nets": list(power_nets),
                "stackup": [
                    {
                        "name": "met1",
                        "layer": "1/0",
                        "label_layer": "1/5",
                        "sheet_resistance_ohm_per_sq": 0.1,
                    },
                    {
                        "name": "met2",
                        "layer": "2/0",
                        "sheet_resistance_ohm_per_sq": 0.05,
                    },
                ],
                "vias": [
                    {
                        "name": "via1",
                        "layer": "3/0",
                        "between": ["met1", "met2"],
                        "resistance_ohm": 5.0,
                    }
                ],
            }
        )
    )


# --- run_power: basic extraction ------------------------------------------


def test_run_power_reports_three_islands_across_two_nets(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_power(str(gds), str(spec))

    assert report["schema_version"] == 1
    assert report["file"] == str(gds)
    assert report["spec"] == str(spec)
    assert report["power_nets"] == ["VPWR", "VGND"]
    assert report["island_count"] == 3
    assert report["node_count"] == 8
    assert report["edge_count"] == 5
    assert report["warnings"] == []

    by_net = {entry["net"]: entry for entry in report["networks"]}
    assert by_net["VPWR"]["island_count"] == 2
    assert by_net["VGND"]["island_count"] == 1


def test_run_power_isolated_island_is_one_rail_edge(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_power(str(gds), str(spec))
    vpwr = next(entry for entry in report["networks"] if entry["net"] == "VPWR")
    island_a = next(i for i in vpwr["islands"] if i["node_count"] == 2)

    assert island_a["edge_count"] == 1
    edge = island_a["edges"][0]
    assert edge["kind"] == "metal"
    assert edge["layer"] == "met1"
    # sheet_resistance_ohm_per_sq (0.1) * length_um (10) / width_um (1)
    assert edge["resistance_ohm"] == pytest.approx(1.0)

    nodes_by_id = {node["id"]: node for node in island_a["nodes"]}
    assert nodes_by_id[edge["from"]]["x_um"] == pytest.approx(0.0)
    assert nodes_by_id[edge["to"]]["x_um"] == pytest.approx(10.0)


def test_run_power_via_bridged_island_has_metal_and_via_edges(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_power(str(gds), str(spec))
    vpwr = next(entry for entry in report["networks"] if entry["net"] == "VPWR")
    island_b = next(i for i in vpwr["islands"] if i["node_count"] == 4)

    assert island_b["edge_count"] == 3
    kinds = sorted(edge["kind"] for edge in island_b["edges"])
    assert kinds == ["metal", "metal", "via"]

    via_edge = next(edge for edge in island_b["edges"] if edge["kind"] == "via")
    assert via_edge["layer"] == "via1"
    assert via_edge["resistance_ohm"] == pytest.approx(5.0)

    met2_edge = next(
        edge
        for edge in island_b["edges"]
        if edge["kind"] == "metal" and edge["layer"] == "met2"
    )
    # 0.05 ohm/sq * length_um (5) / width_um (2)
    assert met2_edge["resistance_ohm"] == pytest.approx(0.125)

    # met2 has no label of its own -- its node still carries the "met2"
    # layer tag, and the whole cluster inherited the "VPWR" net name
    # through via connectivity alone.
    layers = {node["layer"] for node in island_b["nodes"]}
    assert layers == {"met1", "met2"}


# --- run_power: a via taps the polygon it lands on (issue #2259) -----------


def _trunk_and_riser_fixture(path) -> None:
    """The trunk-and-stub topology a whole-net nearest-node search gets
    wrong (issue #2259) -- physically **one** net:

    - one 40 x 0.5 um horizontal `Metal1` trunk at y = 0,
    - three 10 um `Poly2` risers hanging off it at x = 5/20/35, each with a
      `Contact` joining its top to the trunk,
    - a short vertical `Metal1` stub at each riser's bottom, with its own
      `Contact`.

    The middle riser's trunk contact (20, 0) is 20 um from either trunk
    *endpoint* but only 10 um from its own stub's node, so a search over
    every `Metal1` rail on the net wires the trunk tap to the stub: the
    trunk is orphaned from the risers it feeds, and the stub is shorted to
    a rail it does not touch.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TRUNK_RISER")

    poly2 = layout.layer(30, 0)
    metal1 = layout.layer(34, 0)
    contact = layout.layer(33, 0)
    metal1_label = layout.layer(34, 10)

    top.shapes(metal1).insert(kdb.Box.new(_um(0), _um(-0.25), _um(40), _um(0.25)))
    for x in (5, 20, 35):
        top.shapes(poly2).insert(
            kdb.Box.new(_um(x - 0.2), _um(-10), _um(x + 0.2), _um(0))
        )
        top.shapes(contact).insert(
            kdb.Box.new(_um(x - 0.11), _um(-0.11), _um(x + 0.11), _um(0.11))
        )
        top.shapes(metal1).insert(
            kdb.Box.new(_um(x - 0.2), _um(-14), _um(x + 0.2), _um(-10))
        )
        top.shapes(contact).insert(
            kdb.Box.new(_um(x - 0.11), _um(-10.11), _um(x + 0.11), _um(-9.89))
        )
    top.shapes(metal1_label).insert(kdb.Text("vdd", kdb.Trans(_um(2), _um(0))))

    layout.write(str(path))


def _trunk_and_riser_spec(path, *, ir: bool = False) -> None:
    spec = {
        "power_nets": ["vdd"],
        "stackup": [
            {
                "name": "Poly2",
                "layer": "30/0",
                "sheet_resistance_ohm_per_sq": 7.3,
            },
            {
                "name": "Metal1",
                "layer": "34/0",
                "label_layer": "34/10",
                "sheet_resistance_ohm_per_sq": 0.09,
            },
        ],
        "vias": [
            {
                "name": "Contact",
                "layer": "33/0",
                "between": ["Poly2", "Metal1"],
                "resistance_ohm": 8.0,
            }
        ],
    }
    if ir:
        # Pad on the trunk; load at the bottom of the *middle* riser -- the
        # one whose trunk contact the whole-net search mis-attached.
        spec["pads"] = [
            {"name": "pad", "net": "vdd", "x_um": 2.0, "y_um": 0.0, "voltage_v": 3.3}
        ]
        spec["current_model"] = {
            "supply_net": "vdd",
            "instances": [
                {"name": "load", "x_um": 20.0, "y_um": -12.0, "current_a": 1e-3}
            ],
        }
    path.write_text(json.dumps(spec))


def _component_count(island) -> int:
    """How many connected components one island's emitted network has, by
    breadth-first search -- deliberately an independent implementation of
    `power._connected_component_count`, so this asserts the invariant rather
    than re-running the code under test against itself."""
    adjacency = {node["id"]: set() for node in island["nodes"]}
    for edge in island["edges"]:
        adjacency[edge["from"]].add(edge["to"])
        adjacency[edge["to"]].add(edge["from"])
    seen: set[str] = set()
    components = 0
    for start in adjacency:
        if start in seen:
            continue
        components += 1
        queue = [start]
        seen.add(start)
        while queue:
            for neighbour in adjacency[queue.pop()]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
    return components


def test_via_tap_attaches_to_the_polygon_it_lands_on(tmp_path):
    """Each `Contact` is wired to an endpoint of the merged polygon it
    physically lands on, not to whatever node happens to be nearest on the
    whole net (issue #2259).

    The discriminator is the middle riser's trunk contact at (20, 0): its
    own polygon's endpoints are 20 um away (either end of the trunk), while
    an unrelated stub's node is 10 um away. Scoped correctly it lands on the
    trunk; searched net-wide it lands on the stub.
    """
    gds = tmp_path / "trunk_riser.gds"
    spec = tmp_path / "trunk_riser.power.json"
    _trunk_and_riser_fixture(gds)
    _trunk_and_riser_spec(spec)

    report = run_power(str(gds), str(spec))

    assert report["island_count"] == 1
    island = report["networks"][0]["islands"][0]
    nodes = {node["id"]: node for node in island["nodes"]}
    vias = [edge for edge in island["edges"] if edge["kind"] == "via"]
    assert len(vias) == 6

    # The three contacts drawn on the trunk (y = 0) each reach a trunk
    # endpoint -- x = 0 or x = 40, y = 0 -- never a stub node at y = -10.
    trunk_taps = [
        edge
        for edge in vias
        if nodes[edge["from"]]["y_um"] == pytest.approx(0.0)
        or nodes[edge["to"]]["y_um"] == pytest.approx(0.0)
    ]
    assert len(trunk_taps) == 3
    for edge in trunk_taps:
        metal = next(
            nodes[end]
            for end in (edge["from"], edge["to"])
            if nodes[end]["layer"] == "Metal1"
        )
        assert metal["y_um"] == pytest.approx(0.0)
        assert metal["x_um"] in (pytest.approx(0.0), pytest.approx(40.0))

    # ... and each stub contact reaches its *own* stub, not another riser's.
    stub_taps = [edge for edge in vias if edge not in trunk_taps]
    for edge in stub_taps:
        poly, metal = (
            nodes[edge["from"]],
            nodes[edge["to"]],
        )
        if poly["layer"] != "Poly2":
            poly, metal = metal, poly
        assert metal["x_um"] == pytest.approx(poly["x_um"])
        assert metal["y_um"] == pytest.approx(poly["y_um"])


def test_extracted_network_has_one_component_per_island(tmp_path):
    """The invariant that catches this whole class outright (issue #2259):
    `LayoutToNetlist` already decided this geometry is **one** electrically
    connected island, so the resistor network built from it must be one
    connected component. Before the fix it was two -- one of them holding a
    cycle (riser -> stub -> riser) that the layout does not contain."""
    gds = tmp_path / "trunk_riser.gds"
    spec = tmp_path / "trunk_riser.power.json"
    _trunk_and_riser_fixture(gds)
    _trunk_and_riser_spec(spec)

    report = run_power(str(gds), str(spec))

    assert report["island_count"] == 1
    assert [
        _component_count(island) for island in report["networks"][0]["islands"]
    ] == [1]
    assert report["warnings"] == []


def test_trunk_and_riser_ir_drop_reaches_the_loaded_riser(tmp_path):
    """The end-to-end symptom: a load on a riser the pad's trunk really
    feeds is solved, instead of being reported `no_pad` behind a headline
    `worst_case_droop_mv` of 0.0 -- a false clean pass (issue #2259)."""
    gds = tmp_path / "trunk_riser.gds"
    spec = tmp_path / "trunk_riser.power.json"
    _trunk_and_riser_fixture(gds)
    _trunk_and_riser_spec(spec, ir=True)

    report = run_power(str(gds), str(spec))

    assert report["warnings"] == []
    islands = report["ir_drop_map"]["nets"][0]["islands"]
    assert [island["solved"] for island in islands] == [True]
    assert all(node["voltage_v"] is not None for node in islands[0]["nodes"])
    assert report["ir_drop_map"]["unsolved_current_a"] == pytest.approx(0.0)

    # 1 mA through the trunk's left half (3.6 ohm), a Contact (8), the
    # middle riser (182.5), a second Contact (8) and the stub (0.9): the
    # deepest droop is at the far end of the riser, not 0 mV.
    assert report["worst_case_droop_mv"] == pytest.approx(190.5, rel=1e-6)
    worst = report["ir_drop_map"]["worst_case"]
    assert (worst["x_um"], worst["y_um"]) == (pytest.approx(20.0), pytest.approx(-10.0))


def test_via_landing_on_no_modelled_segment_is_skipped_with_a_warning(tmp_path):
    """A via shape that lands on no polygon of one of the roles it claims to
    bridge is skipped and named, rather than wired across to a rail it never
    touches (issue #2259).

    The fixture is the `_basic_fixture` island B topology plus a *stray* via
    shape sitting on met1 alone, at the far end of the rail from the real
    met1->met2 stack. It is still on the net (it touches met1), so it is
    still returned by `polygons_of_net`; before the fix it invented a second
    met1->met2 edge to the stub 4 um away.
    """
    gds = tmp_path / "stray_via.gds"
    spec = tmp_path / "basic.power.json"

    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    met2 = layout.layer(2, 0)
    via1 = layout.layer(3, 0)
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(5), _um(10), _um(6)))
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(5), _um(5.5))))
    top.shapes(via1).insert(kdb.Box.new(_um(4), _um(5), _um(6), _um(6)))
    top.shapes(met2).insert(kdb.Box.new(_um(4), _um(5), _um(6), _um(10)))
    # The stray one: on met1, under no met2 at all.
    top.shapes(via1).insert(kdb.Box.new(_um(9), _um(5), _um(10), _um(6)))
    layout.write(str(gds))
    _basic_spec(spec, power_nets=("VPWR",))

    report = run_power(str(gds), str(spec))

    island = report["networks"][0]["islands"][0]
    assert [edge["kind"] for edge in island["edges"]].count("via") == 1
    skipped = [w for w in report["warnings"] if "lands on no modelled" in w]
    assert len(skipped) == 1
    assert "'met2'" in skipped[0]
    # Skipping the phantom via costs nothing real: the island is still one
    # connected component, because the genuine via still bridges it.
    assert _component_count(island) == 1


def test_run_power_island_ids_are_scoped_per_net(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_power(str(gds), str(spec))
    vpwr = next(entry for entry in report["networks"] if entry["net"] == "VPWR")
    vgnd = next(entry for entry in report["networks"] if entry["net"] == "VGND")

    assert {i["island_id"] for i in vpwr["islands"]} == {"VPWR#0", "VPWR#1"}
    assert {i["island_id"] for i in vgnd["islands"]} == {"VGND#0"}


# --- run_power: unmatched nets --------------------------------------------


def test_run_power_one_unmatched_net_is_a_warning_not_a_failure(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec, power_nets=("VPWR", "NOPE"))

    report = run_power(str(gds), str(spec))
    assert report["island_count"] == 2  # only VPWR's two islands
    nope = next(entry for entry in report["networks"] if entry["net"] == "NOPE")
    assert nope == {
        "net": "NOPE",
        "island_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "islands": [],
    }
    assert any(
        "NOPE" in w and "matches no labelled net" in w for w in report["warnings"]
    )


def test_run_power_every_net_unmatched_raises(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec, power_nets=("NOPE1", "NOPE2"))

    with pytest.raises(PowerError, match="none of the requested"):
        run_power(str(gds), str(spec))


# --- run_power: matching a net by a label it carries (issue #2171) ---------


def _multi_label_fixture(path) -> None:
    """One met1 rail carrying **two** labels -- a chip-level `VDD_CORE` bus
    landing on a pad cell's own `DVDD` plate, the shape issue #2171 reports.

    KLayout names such a net after *all* its labels, comma-joined
    (`expanded_name()` == `"DVDD,VDD_CORE"`), so an exact-name spec entry
    asking for `VDD_CORE` used to match nothing.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)

    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(10), _um(1)))
    top.shapes(met1_label).insert(kdb.Text("VDD_CORE", kdb.Trans(_um(2), _um(0.5))))
    top.shapes(met1_label).insert(kdb.Text("DVDD", kdb.Trans(_um(8), _um(0.5))))

    # A second, single-labelled net so the "names actually present" list has
    # more than one entry to report.
    top.shapes(met1).insert(kdb.Box.new(_um(20), _um(0), _um(30), _um(1)))
    top.shapes(met1_label).insert(kdb.Text("VSS", kdb.Trans(_um(25), _um(0.5))))

    layout.write(str(path))


def _label_spec(path, *, power_nets) -> None:
    path.write_text(
        json.dumps(
            {
                "power_nets": list(power_nets),
                "stackup": [
                    {
                        "name": "met1",
                        "layer": "1/0",
                        "label_layer": "1/5",
                        "sheet_resistance_ohm_per_sq": 0.1,
                    }
                ],
            }
        )
    )


def test_run_power_matches_a_net_by_a_label_it_carries(tmp_path):
    gds = tmp_path / "pads.gds"
    spec = tmp_path / "pads.power.json"
    _multi_label_fixture(gds)
    _label_spec(spec, power_nets=("VDD_CORE",))

    report = run_power(str(gds), str(spec))

    assert report["warnings"] == []
    assert report["island_count"] == 1
    vdd = next(entry for entry in report["networks"] if entry["net"] == "VDD_CORE")
    assert vdd["island_count"] == 1
    # The key stays the caller's own name, not KLayout's comma-joined one.
    assert report["power_nets"] == ["VDD_CORE"]


def test_run_power_matches_the_other_label_of_the_same_net_too(tmp_path):
    gds = tmp_path / "pads.gds"
    spec = tmp_path / "pads.power.json"
    _multi_label_fixture(gds)
    _label_spec(spec, power_nets=("DVDD",))

    report = run_power(str(gds), str(spec))
    assert report["island_count"] == 1


def test_run_power_full_comma_joined_name_still_matches(tmp_path):
    gds = tmp_path / "pads.gds"
    spec = tmp_path / "pads.power.json"
    _multi_label_fixture(gds)
    _label_spec(spec, power_nets=("DVDD,VDD_CORE",))

    report = run_power(str(gds), str(spec))
    assert report["island_count"] == 1


def test_run_power_exact_match_mode_rejects_a_bare_label(tmp_path):
    gds = tmp_path / "pads.gds"
    spec = tmp_path / "pads.power.json"
    _multi_label_fixture(gds)
    _label_spec(
        spec,
        power_nets=(
            {"name": "VDD_CORE", "match": "exact"},
            {"name": "VSS", "match": "exact"},
        ),
    )

    report = run_power(str(gds), str(spec))
    vdd = next(entry for entry in report["networks"] if entry["net"] == "VDD_CORE")
    assert vdd["island_count"] == 0
    vss = next(entry for entry in report["networks"] if entry["net"] == "VSS")
    assert vss["island_count"] == 1
    assert any(
        "VDD_CORE" in w and "matches no labelled net" in w for w in report["warnings"]
    )


def test_run_power_rejects_an_unknown_match_mode(tmp_path):
    gds = tmp_path / "pads.gds"
    spec = tmp_path / "pads.power.json"
    _multi_label_fixture(gds)
    _label_spec(spec, power_nets=({"name": "VDD_CORE", "match": "regex"},))

    with pytest.raises(PowerError, match="'match' must be one of"):
        run_power(str(gds), str(spec))


def test_run_power_rejects_a_power_net_object_without_a_name(tmp_path):
    gds = tmp_path / "pads.gds"
    spec = tmp_path / "pads.power.json"
    _multi_label_fixture(gds)
    _label_spec(spec, power_nets=({"match": "exact"},))

    with pytest.raises(PowerError, match="must be a non-empty string or an object"):
        run_power(str(gds), str(spec))


def test_run_power_unmatched_net_warning_lists_the_net_names_present(tmp_path):
    gds = tmp_path / "pads.gds"
    spec = tmp_path / "pads.power.json"
    _multi_label_fixture(gds)
    _label_spec(spec, power_nets=("VDD_CORE", "NOPE"))

    report = run_power(str(gds), str(spec))
    warning = next(w for w in report["warnings"] if "NOPE" in w)
    assert "nets actually present" in warning
    assert "DVDD,VDD_CORE" in warning
    assert "VSS" in warning


def test_run_power_every_net_unmatched_error_lists_the_net_names_present(tmp_path):
    gds = tmp_path / "pads.gds"
    spec = tmp_path / "pads.power.json"
    _multi_label_fixture(gds)
    _label_spec(spec, power_nets=("NOPE1", "NOPE2"))

    with pytest.raises(PowerError) as excinfo:
        run_power(str(gds), str(spec))
    message = str(excinfo.value)
    assert "none of the requested" in message
    assert "nets actually present" in message
    assert "DVDD,VDD_CORE" in message
    assert "VSS" in message


# --- run_power: non-rectangular polygon decomposition (issue #2171) --------


def _l_bus_fixture(path) -> None:
    """One `VPWR` net drawn as an L, the normal shape of a real PDN ring:

    - a horizontal arm x:[0,100] y:[0,20] um, and
    - a vertical arm x:[0,20] y:[20,100] um

    merging into a single L-shaped polygon whose bounding box (100x100 um)
    is five times wider than either arm is drawn. At 0.1 ohm/sq the bounding
    box is **one** square (0.1 ohm) while the real L is ~9 squares.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(100), _um(20)))
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(20), _um(20), _um(100)))
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(50), _um(10))))
    layout.write(str(path))


def _l_bus_spec(path, *, pads=None, current_model=None) -> None:
    payload = {
        "power_nets": ["VPWR"],
        "stackup": [
            {
                "name": "met1",
                "layer": "1/0",
                "label_layer": "1/5",
                "sheet_resistance_ohm_per_sq": 0.1,
            }
        ],
    }
    if pads is not None:
        payload["pads"] = pads
    if current_model is not None:
        payload["current_model"] = current_model
    path.write_text(json.dumps(payload))


def _total_series_resistance(island, frm, to) -> float:
    """Sum the resistances along the unique path between two nodes of a
    tree-shaped island (the L bus below has no loops)."""
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for edge in island["edges"]:
        adjacency.setdefault(edge["from"], []).append(
            (edge["to"], edge["resistance_ohm"])
        )
        adjacency.setdefault(edge["to"], []).append(
            (edge["from"], edge["resistance_ohm"])
        )
    stack = [(frm, None, 0.0)]
    while stack:
        node, parent, total = stack.pop()
        if node == to:
            return total
        for neighbour, resistance in adjacency.get(node, []):
            if neighbour != parent:
                stack.append((neighbour, node, total + resistance))
    raise AssertionError(f"no path from {frm} to {to}")


def test_run_power_l_shaped_segment_is_decomposed_not_bbox_approximated(tmp_path):
    gds = tmp_path / "lshape.gds"
    spec = tmp_path / "lshape.power.json"
    _l_bus_fixture(gds)
    _l_bus_spec(spec)

    report = run_power(str(gds), str(spec))
    assert report["island_count"] == 1
    island = report["networks"][0]["islands"][0]
    assert island["unsolved_reason"] is None

    # The bounding-box model produced exactly one edge for the whole L; the
    # decomposition produces one per sub-segment.
    assert island["edge_count"] > 1

    nodes_by_xy = {(n["x_um"], n["y_um"]): n["id"] for n in island["nodes"]}
    far_x = nodes_by_xy[(100.0, 10.0)]  # open end of the horizontal arm
    far_y = nodes_by_xy[(10.0, 100.0)]  # open end of the vertical arm
    # 9 squares * 0.1 ohm/sq along the real L, not the 1 square (0.1 ohm)
    # its 100x100 um bounding box would have claimed.
    assert _total_series_resistance(island, far_x, far_y) == pytest.approx(0.9)

    # The pre-#2171 "approximated by its bounding box" warning is gone: the
    # segment is modelled, not approximated.
    assert not any("approximated by its bounding box" in w for w in report["warnings"])
    assert any("non-rectangular" in w and "decomposed" in w for w in report["warnings"])


def test_run_power_l_shaped_bus_droop_reflects_the_real_arm_widths(tmp_path):
    gds = tmp_path / "lshape.gds"
    spec = tmp_path / "lshape.power.json"
    _l_bus_fixture(gds)
    _l_bus_spec(
        spec,
        pads=[
            {"name": "P0", "net": "VPWR", "x_um": 100.0, "y_um": 10.0, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [
                {"name": "u0", "x_um": 10.0, "y_um": 100.0, "current_a": 0.1}
            ],
        },
    )

    report = run_power(str(gds), str(spec))
    # 0.1 A through 0.9 ohm = 90 mV; the bounding-box model reported 10 mV
    # for the same geometry -- understating droop nine-fold.
    assert report["worst_case_droop_mv"] == pytest.approx(90.0)


def test_run_power_declares_an_island_unsolved_over_the_decomposition_cap(
    tmp_path, monkeypatch
):
    import klayout_tools.power as power_module

    monkeypatch.setattr(power_module, "MAX_DECOMPOSITION_CELLS", 1)

    gds = tmp_path / "lshape.gds"
    spec = tmp_path / "lshape.power.json"
    _l_bus_fixture(gds)
    _l_bus_spec(spec)

    report = run_power(str(gds), str(spec))
    island = report["networks"][0]["islands"][0]
    assert island["nodes"] == []
    assert island["edges"] == []
    assert island["unsolved_reason"] is not None
    assert "mesh cell" in island["unsolved_reason"]
    assert any("unsolved" in w for w in report["warnings"])


def test_run_power_declares_a_non_manhattan_segment_unsolved(tmp_path):
    gds = tmp_path / "diagonal.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    top.shapes(met1).insert(
        kdb.Polygon(
            [
                kdb.Point(_um(0), _um(0)),
                kdb.Point(_um(100), _um(0)),
                kdb.Point(_um(100), _um(100)),
            ]
        )
    )
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(80), _um(10))))
    layout.write(str(gds))

    spec = tmp_path / "diagonal.power.json"
    _l_bus_spec(spec)

    report = run_power(str(gds), str(spec))
    island = report["networks"][0]["islands"][0]
    assert island["unsolved_reason"] is not None
    assert "axis-aligned" in island["unsolved_reason"]
    assert island["edges"] == []


def test_run_power_rectangular_segments_keep_the_single_edge_model(tmp_path):
    """The decomposition is scoped to non-rectangular polygons: a plain
    box rail is still one edge between two endpoint nodes."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_power(str(gds), str(spec))
    assert report["node_count"] == 8
    assert report["edge_count"] == 5
    for net_entry in report["networks"]:
        for island in net_entry["islands"]:
            assert island["unsolved_reason"] is None


def _stepped_ledge_fixture(path) -> None:
    """One `VPWR` net: a horizontal bar `x:[0,100] y:[0,5]` with a small
    ledge raised on its right half, `x:[50,100] y:[5,7]`, merging into one
    Manhattan polygon. The ledge's own vertices (`x=50`) become a *global*
    cut line across the whole merged polygon, so the bar's `row0` decomposes
    into two cells split at `x=50` -- and the ledge's own cell, `(col1,
    row1) = x:[50,100] y:[5,7]` (50 um wide, 2 um tall) is wider than it is
    tall, even though its only neighbour is *below* it (a vertical
    connection). A per-cell width-vs-height test misreads this as
    horizontal-flowing, which drops its true free end (the ledge's top,
    `(75, 7)`) and fabricates two spurious port nodes on its left/right
    walls (`(50, 6)` and `(100, 6)`) instead (issue #2190)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(100), _um(5)))
    top.shapes(met1).insert(kdb.Box.new(_um(50), _um(5), _um(100), _um(7)))
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(10), _um(2.5))))
    layout.write(str(path))


def test_run_power_mesh_terminal_end_uses_neighbour_flow_not_cell_aspect(tmp_path):
    gds = tmp_path / "ledge.gds"
    spec = tmp_path / "ledge.power.json"
    _stepped_ledge_fixture(gds)
    _l_bus_spec(spec)

    report = run_power(str(gds), str(spec))
    island = report["networks"][0]["islands"][0]
    assert island["unsolved_reason"] is None

    node_xy = {(n["x_um"], n["y_um"]) for n in island["nodes"]}
    # The ledge's true free end -- its top, the far side from where it joins
    # the main bar -- must get a terminal node.
    assert (75.0, 7.0) in node_xy
    # Its left/right walls are not flow ends (its only neighbour is below,
    # not beside it) and must not fabricate terminal nodes.
    assert (50.0, 6.0) not in node_xy
    assert (100.0, 6.0) not in node_xy


# --- run_power: unsolved-island attachment scoping (issue #2190) -----------


def _l_bus_with_separate_island_fixture(path) -> None:
    """The same `VPWR` L-bus as :func:`_l_bus_fixture`, plus a second,
    electrically disconnected `VPWR` rail far away (`x:[500,510]
    y:[0,1]`) -- so this net has two islands: one that can be forced
    unsolved (the L, via a small `MAX_DECOMPOSITION_CELLS`) and one that
    always solves normally."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(100), _um(20)))
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(20), _um(20), _um(100)))
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(50), _um(10))))
    top.shapes(met1).insert(kdb.Box.new(_um(500), _um(0), _um(510), _um(1)))
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(505), _um(0.5))))
    layout.write(str(path))


def test_run_power_current_on_an_unsolved_island_is_not_misattached(
    tmp_path, monkeypatch
):
    import klayout_tools.power as power_module

    monkeypatch.setattr(power_module, "MAX_DECOMPOSITION_CELLS", 1)

    gds = tmp_path / "lshape_plus.gds"
    spec = tmp_path / "lshape_plus.power.json"
    _l_bus_with_separate_island_fixture(gds)
    _l_bus_spec(
        spec,
        pads=[
            {"name": "P0", "net": "VPWR", "x_um": 505.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"name": "u0", "x_um": 10.0, "y_um": 10.0, "current_a": 0.1}],
        },
    )

    report = run_power(str(gds), str(spec))
    islands = report["networks"][0]["islands"]
    unsolved = next(i for i in islands if i["unsolved_reason"] is not None)
    solved = next(i for i in islands if i["unsolved_reason"] is None)

    ir_drop_map = report["ir_drop_map"]
    # `u0`'s 0.1 A sits squarely on the unsolved L -- it must be counted as
    # unsolved, never silently reattached to the far, electrically unrelated
    # solved island.
    assert ir_drop_map["unsolved_current_a"] == pytest.approx(0.1)
    solved_net = next(
        island
        for island in ir_drop_map["nets"][0]["islands"]
        if island["island_id"] == solved["island_id"]
    )
    assert solved_net["current_a"] == pytest.approx(0.0)
    assert solved_net["instance_count"] == 0
    assert any(
        "u0" in w and unsolved["island_id"] in w and "unsolved_current_a" in w
        for w in report["warnings"]
    )


def test_run_power_pad_on_an_unsolved_island_is_not_attached_elsewhere(
    tmp_path, monkeypatch
):
    import klayout_tools.power as power_module

    monkeypatch.setattr(power_module, "MAX_DECOMPOSITION_CELLS", 1)

    gds = tmp_path / "lshape_plus.gds"
    spec = tmp_path / "lshape_plus.power.json"
    _l_bus_with_separate_island_fixture(gds)
    _l_bus_spec(
        spec,
        pads=[
            {
                "name": "Pbad",
                "net": "VPWR",
                "x_um": 10.0,
                "y_um": 10.0,
                "voltage_v": 1.8,
            }
        ],
    )

    report = run_power(str(gds), str(spec))
    islands = report["networks"][0]["islands"]
    unsolved = next(i for i in islands if i["unsolved_reason"] is not None)
    solved = next(i for i in islands if i["unsolved_reason"] is None)

    ir_drop_map = report["ir_drop_map"]
    pad_report = next(p for p in ir_drop_map["pads"] if p["name"] == "Pbad")
    # The pad geometrically sits on the unsolved island -- it must be
    # reported as belonging there (with no node to attach to), never
    # silently attached to the far, electrically unrelated solved island.
    assert pad_report["island_id"] == unsolved["island_id"]
    assert pad_report["node_id"] is None
    solved_net = next(
        island
        for island in ir_drop_map["nets"][0]["islands"]
        if island["island_id"] == solved["island_id"]
    )
    assert solved_net["pad_count"] == 0
    assert any("Pbad" in w and "unsolved" in w for w in report["warnings"])


# --- run_power: top-cell selection ------------------------------------------


def test_run_power_requires_top_when_multiple_top_cells(tmp_path):
    gds = tmp_path / "two_tops.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    first = layout.create_cell("FIRST")
    layout.create_cell("SECOND")
    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    first.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(10), _um(1)))
    first.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(5), _um(0.5))))
    layout.write(str(gds))

    spec = tmp_path / "spec.json"
    _basic_spec(spec, power_nets=("VPWR",))

    with pytest.raises(PowerError, match="exactly one top cell"):
        run_power(str(gds), str(spec))

    report = run_power(str(gds), str(spec), top="FIRST")
    assert report["island_count"] == 1


# --- run_power: file/spec errors -------------------------------------------


def test_run_power_missing_file_raises(tmp_path):
    spec = tmp_path / "spec.json"
    _basic_spec(spec)
    with pytest.raises(PowerError, match="file not found"):
        run_power(str(tmp_path / "nope.gds"), str(spec))


def test_run_power_missing_spec_raises(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    with pytest.raises(PowerError, match="spec file not found"):
        run_power(str(gds), str(tmp_path / "nope.json"))


# --- run_power: spec validation --------------------------------------------


def _write_spec(path, payload) -> None:
    path.write_text(json.dumps(payload))


def test_spec_requires_non_empty_power_nets(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "power_nets": [],
            "stackup": [
                {
                    "name": "met1",
                    "layer": "1/0",
                    "label_layer": "1/5",
                    "sheet_resistance_ohm_per_sq": 0.1,
                }
            ],
        },
    )
    with pytest.raises(PowerError, match="non-empty 'power_nets'"):
        run_power(str(gds), str(spec))


def test_spec_requires_non_empty_stackup(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(spec, {"power_nets": ["VPWR"], "stackup": []})
    with pytest.raises(PowerError, match="non-empty 'stackup'"):
        run_power(str(gds), str(spec))


def test_spec_requires_at_least_one_label_layer(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "power_nets": ["VPWR"],
            "stackup": [
                {"name": "met1", "layer": "1/0", "sheet_resistance_ohm_per_sq": 0.1}
            ],
        },
    )
    with pytest.raises(
        PowerError, match="at least one 'stackup' entry must set 'label_layer'"
    ):
        run_power(str(gds), str(spec))


def test_spec_rejects_duplicate_stackup_names(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "power_nets": ["VPWR"],
            "stackup": [
                {
                    "name": "met1",
                    "layer": "1/0",
                    "label_layer": "1/5",
                    "sheet_resistance_ohm_per_sq": 0.1,
                },
                {"name": "met1", "layer": "2/0", "sheet_resistance_ohm_per_sq": 0.1},
            ],
        },
    )
    with pytest.raises(PowerError, match="duplicate stackup name"):
        run_power(str(gds), str(spec))


def test_spec_rejects_negative_sheet_resistance(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "power_nets": ["VPWR"],
            "stackup": [
                {
                    "name": "met1",
                    "layer": "1/0",
                    "label_layer": "1/5",
                    "sheet_resistance_ohm_per_sq": -1.0,
                }
            ],
        },
    )
    with pytest.raises(PowerError, match="sheet_resistance_ohm_per_sq"):
        run_power(str(gds), str(spec))


def test_spec_rejects_via_between_unknown_stackup_name(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "power_nets": ["VPWR"],
            "stackup": [
                {
                    "name": "met1",
                    "layer": "1/0",
                    "label_layer": "1/5",
                    "sheet_resistance_ohm_per_sq": 0.1,
                }
            ],
            "vias": [
                {"layer": "3/0", "between": ["met1", "met2"], "resistance_ohm": 1.0}
            ],
        },
    )
    with pytest.raises(PowerError, match="must name two distinct 'stackup' entries"):
        run_power(str(gds), str(spec))


def test_spec_rejects_via_between_same_name_twice(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "power_nets": ["VPWR"],
            "stackup": [
                {
                    "name": "met1",
                    "layer": "1/0",
                    "label_layer": "1/5",
                    "sheet_resistance_ohm_per_sq": 0.1,
                }
            ],
            "vias": [
                {"layer": "3/0", "between": ["met1", "met1"], "resistance_ohm": 1.0}
            ],
        },
    )
    with pytest.raises(PowerError, match="must name two distinct 'stackup' entries"):
        run_power(str(gds), str(spec))


def test_spec_rejects_negative_via_resistance(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "power_nets": ["VPWR"],
            "stackup": [
                {
                    "name": "met1",
                    "layer": "1/0",
                    "label_layer": "1/5",
                    "sheet_resistance_ohm_per_sq": 0.1,
                },
                {"name": "met2", "layer": "2/0", "sheet_resistance_ohm_per_sq": 0.1},
            ],
            "vias": [
                {"layer": "3/0", "between": ["met1", "met2"], "resistance_ohm": -2.0}
            ],
        },
    )
    with pytest.raises(PowerError, match="resistance_ohm"):
        run_power(str(gds), str(spec))


def test_spec_rejects_malformed_layer_string(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "power_nets": ["VPWR"],
            "stackup": [
                {
                    "name": "met1",
                    "layer": "not-a-layer",
                    "label_layer": "1/5",
                    "sheet_resistance_ohm_per_sq": 0.1,
                }
            ],
        },
    )
    with pytest.raises(PowerError, match="must be '<layer>/<datatype>'"):
        run_power(str(gds), str(spec))


# --- CLI --------------------------------------------------------------------


def test_cli_json_contract(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["power", str(gds), str(spec), "--format", "json"]) == 4
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "coverage",
        "status",
        "schema_version",
        "file",
        "spec",
        "power_nets",
        "networks",
        "node_count",
        "edge_count",
        "island_count",
        # Added additively by #845 (Phase 1b) and #846 (Phase 1c); all three
        # are null for a spec that declares neither `pads` nor a
        # `current_model`, so every field Phase 1a documented is unchanged --
        # hence no `schema_version` bump.
        "ir_drop_map",
        "worst_case_droop_mv",
        "em_verdict",
        "warnings",
    }
    assert data["ir_drop_map"] is None
    assert data["worst_case_droop_mv"] is None
    assert data["em_verdict"] is None
    assert data["schema_version"] == 1


def test_cli_text_output(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["power", str(gds), str(spec)]) == 4
    out = capsys.readouterr().out
    assert "net VPWR: 2 island(s)" in out
    assert "net VGND: 1 island(s)" in out
    assert "VPWR#0" in out


def test_cli_text_output_renders_warnings(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec, power_nets=("VPWR", "NOPE"))

    assert main(["power", str(gds), str(spec)]) == 4
    out = capsys.readouterr().out
    assert "warnings:" in out
    assert "NOPE" in out


def test_cli_error_exits_one_with_clean_message(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)

    assert main(["power", str(gds), str(tmp_path / "nope.json")]) == 1
    err = capsys.readouterr().err
    assert "spec file not found" in err
    assert "Traceback" not in err


def test_cli_json_error_shape(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)

    assert (
        main(["power", str(gds), str(tmp_path / "nope.json"), "--format", "json"]) == 1
    )
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "power"
    assert "spec file not found" in err["error"]["message"]


# --- Acceptance: real klt par output (issue #844's own criterion) ----------


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)
def test_gcd_fixture_extracts_a_real_power_grid(tmp_path):
    """`klt power` against the real, OpenROAD-produced GCD macro-scale
    fixture (the same `tests/corpus/place_and_route/gcd.gds.gz` `test_drc.py`/
    `test_extract.py`/`test_lvs.py` already validate against): stackup =
    sky130's own met1 (`68/20`, pin/label `68/5`) + met2 (`69/20`, pin/label
    `69/5`) + via1 (met1<->met2, `68/44`) -- layer/datatype numbers verified
    against a real sky130A install (volare) in `decks/sky130.py`'s own
    `metals`/`metal_labels`/`vias` tuples.

    `klt par` (issue #700) deliberately runs no PDN (power-grid) generation
    (see `place_and_route.py`'s "Deliberately out of scope for this v1"
    note), so this design's power/ground geometry is whatever the standard
    cell rows themselves contribute -- disconnected, un-strapped per-row
    islands rather than one connected mesh (no cross-row straps, no PDN
    grid). The island counts below are a regression pin against this
    specific, static, committed fixture (mirroring `test_drc.py`'s own
    `violation_count` pin) -- not a claim about what a real PDN-equipped
    design should look like.

    Re-pinned for #1443: this fixture was regenerated with #1442's row-rail
    fix applied (an unconditional row-rail obstruction before routing,
    un-gating `filler_placement`). Before that fix, missing filler cells
    left literal gaps in each row's own met1 power rail, so a single row
    often split into *several* separate VPWR/VGND islands (88 VPWR + 105
    VGND across this design's 17 rows, pre-#1443). `filler_placement` now
    closes every one of those gaps, so each row's rail is one continuous
    island per net -- exactly 17 VPWR + 17 VGND islands, matching this
    design's row count 1:1. This is a real, structural improvement in the
    fixture (it is also what makes `klt drc --deck sky130` go from 184
    `nwell.*` violations to 0, per #1430), not an artifact to work around --
    so this test is re-pinned to the new topology rather than split off
    into a separate frozen "no PDN" fixture. The multi-island/fragmented
    topology code paths this test used to exercise at this scale (multiple
    islands per net, per-island pad/current bookkeeping, unsolved-island
    warnings) remain independently covered by hand-drawn, small-scale
    fixtures elsewhere in this file --
    `test_run_power_reports_three_islands_across_two_nets`,
    `test_run_power_isolated_island_is_one_rail_edge`,
    `test_run_power_via_bridged_island_has_metal_and_via_edges`,
    `test_run_power_island_ids_are_scoped_per_net`,
    `test_islands_without_a_pad_are_reported_unsolved_with_one_summary_warning`,
    `test_current_stranded_on_a_padless_island_is_named_and_totalled` --
    so this real-fixture test's own purpose (validate extraction against
    real, machine-generated geometry, not specifically preserve
    fragmentation) is unaffected by re-pinning to the fixed topology.
    """
    spec = tmp_path / "gcd.power.json"
    spec.write_text(
        json.dumps(
            {
                "power_nets": ["VPWR", "VGND"],
                "stackup": [
                    {
                        "name": "met1",
                        "layer": "68/20",
                        "label_layer": "68/5",
                        "sheet_resistance_ohm_per_sq": 0.1,
                    },
                    {
                        "name": "met2",
                        "layer": "69/20",
                        "label_layer": "69/5",
                        "sheet_resistance_ohm_per_sq": 0.05,
                    },
                ],
                "vias": [
                    {
                        "name": "via1",
                        "layer": "68/44",
                        "between": ["met1", "met2"],
                        "resistance_ohm": 2.0,
                    }
                ],
            }
        )
    )

    report = run_power(str(PLACE_AND_ROUTE_GDS), str(spec))

    assert report["warnings"] == []
    by_net = {entry["net"]: entry for entry in report["networks"]}
    assert by_net["VPWR"]["island_count"] == 17
    assert by_net["VGND"]["island_count"] == 17
    assert report["island_count"] == 34
    assert report["node_count"] == 68
    assert report["edge_count"] == 34

    # Every rail is a real, positive resistor -- not a silent zero.
    for entry in report["networks"]:
        for island in entry["islands"]:
            for edge in island["edges"]:
                assert edge["resistance_ohm"] > 0
                assert edge["kind"] in {"metal", "via"}


def _sky130_pdn_stackup_spec(path: Path) -> None:
    """The full `met1`-`met5` sky130 power stackup `gcd-pdn.gds.gz`'s own PDN
    actually occupies -- layer/datatype numbers from `decks/sky130.py`'s own
    `metals`/`metal_labels`/`vias` tuples (verified against a real sky130A
    install), sheet resistances of the same order as the two-metal spec
    above."""
    path.write_text(
        json.dumps(
            {
                "power_nets": ["VPWR", "VGND"],
                "stackup": [
                    {
                        "name": name,
                        "layer": f"{num}/20",
                        "label_layer": f"{num}/5",
                        "sheet_resistance_ohm_per_sq": rsq,
                    }
                    for name, num, rsq in (
                        ("met1", 68, 0.125),
                        ("met2", 69, 0.125),
                        ("met3", 70, 0.047),
                        ("met4", 71, 0.047),
                        ("met5", 72, 0.029),
                    )
                ],
                "vias": [
                    {
                        "name": name,
                        "layer": f"{num}/44",
                        "between": list(between),
                        "resistance_ohm": rohm,
                    }
                    for name, num, between, rohm in (
                        ("via1", 68, ("met1", "met2"), 2.0),
                        ("via2", 69, ("met2", "met3"), 2.0),
                        ("via3", 70, ("met3", "met4"), 2.0),
                        ("via4", 71, ("met4", "met5"), 0.4),
                    )
                ],
            }
        )
    )


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_PDN_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route PDN corpus fixture checked in",
)
def test_gcd_pdn_fixture_resolves_each_supply_to_one_island(tmp_path):
    """The positive control the fixture above is the negative control for
    (issue #2079): the *same* `gcd` design, routed with a real
    `request.power` PDN (ORFS `platforms/sky130hd/pdn.tcl`'s own met1
    followpins rail + met4/met5 straps), resolves each supply to **exactly
    one** island -- one connected mesh, not 17 disconnected per-row rails.

    This is the property the fixture exists for, so it is pinned here rather
    than left to the regeneration script alone: a `request.power` run whose
    grid silently failed to tie the rows together still produces a routable,
    DRC-clean GDS, and the island count is the cheapest place that shows up.
    (`regenerate.sh` additionally gates the fixture on `klt lvs`'s
    `power_connectivity` block -- every standard-cell supply *pin* reaching
    one net -- which needs the run's own as-built Verilog and so cannot be
    re-checked from the committed GDS alone.)

    The second half is the discriminator that makes the first half mean
    something: re-run against only the two-metal `met1`/`met2` stackup the
    gridless fixture's test uses, and this same fixture reports 17 islands
    per net again -- the met4/met5 straps are what connect the rows, and
    `klt power` only sees the layers a spec declares. So "1 island" is a
    statement about this layout's real PDN geometry, not about `klt power`
    having grown more permissive.
    """
    spec = tmp_path / "gcd_pdn.power.json"
    _sky130_pdn_stackup_spec(spec)

    report = run_power(str(PLACE_AND_ROUTE_PDN_GDS), str(spec))

    assert report["warnings"] == []
    by_net = {entry["net"]: entry for entry in report["networks"]}
    assert by_net["VPWR"]["island_count"] == 1
    assert by_net["VGND"]["island_count"] == 1
    assert report["island_count"] == 2
    assert report["node_count"] == 500
    assert report["edge_count"] == 1594

    # Every edge is a real, positive resistor -- and both kinds are present:
    # a mesh tied together purely by metal (no vias) would not be a PDN.
    kinds = set()
    for entry in report["networks"]:
        for island in entry["islands"]:
            for edge in island["edges"]:
                assert edge["resistance_ohm"] > 0
                kinds.add(edge["kind"])
    assert kinds == {"metal", "via"}

    # Discriminator: the same fixture, read through the two-metal stackup
    # `test_gcd_fixture_extracts_a_real_power_grid` uses, is fragmented
    # exactly like the gridless fixture -- the straps that connect the rows
    # live on met4/met5, which that spec never declares.
    two_metal = tmp_path / "gcd_pdn_two_metal.power.json"
    two_metal.write_text(
        json.dumps(
            {
                "power_nets": ["VPWR", "VGND"],
                "stackup": [
                    {
                        "name": "met1",
                        "layer": "68/20",
                        "label_layer": "68/5",
                        "sheet_resistance_ohm_per_sq": 0.1,
                    },
                    {
                        "name": "met2",
                        "layer": "69/20",
                        "label_layer": "69/5",
                        "sheet_resistance_ohm_per_sq": 0.05,
                    },
                ],
                "vias": [
                    {
                        "name": "via1",
                        "layer": "68/44",
                        "between": ["met1", "met2"],
                        "resistance_ohm": 2.0,
                    }
                ],
            }
        )
    )

    partial = run_power(str(PLACE_AND_ROUTE_PDN_GDS), str(two_metal))
    partial_by_net = {entry["net"]: entry for entry in partial["networks"]}
    assert partial_by_net["VPWR"]["island_count"] == 17
    assert partial_by_net["VGND"]["island_count"] == 17


def test_gcd_pdn_network_has_one_component_per_island(tmp_path):
    """Issue #2259's invariant on real PDN geometry: the number of connected
    components in the emitted resistor network equals the number of islands
    the connectivity model found.

    The hand-drawn fixture above pins the failing topology; this pins the
    property on a real `klt par` PDN -- a met1-followpins mesh with met4/met5
    straps and hundreds of via taps, exactly the trunk-and-stub shape a
    whole-net nearest-node search silently fragments."""
    spec = tmp_path / "gcd_pdn.power.json"
    _sky130_pdn_stackup_spec(spec)

    report = run_power(str(PLACE_AND_ROUTE_PDN_GDS), str(spec))

    components = [
        _component_count(island)
        for entry in report["networks"]
        for island in entry["islands"]
    ]
    assert components == [1] * report["island_count"]


# --- Static IR-drop solve (issue #845, Phase 1b) ----------------------------
#
# The solver's own correctness is validated against closed-form resistive
# networks in `tests/test_ir_solver.py` and cross-checked against ngspice in
# `tests/test_power_ir_cross_check.py`. What follows is the *binding*: that a
# spec's `pads`/`current_model` attach to the extracted geometry the way
# `docs/cli/power.md` says they do, and that the response says so.


def _ir_spec(path, *, pads, current_model=None, power_nets=("VPWR", "VGND")) -> None:
    spec = {
        "power_nets": list(power_nets),
        "stackup": [
            {
                "name": "met1",
                "layer": "1/0",
                "label_layer": "1/5",
                "sheet_resistance_ohm_per_sq": 0.1,
            },
            {
                "name": "met2",
                "layer": "2/0",
                "sheet_resistance_ohm_per_sq": 0.05,
            },
        ],
        "vias": [
            {
                "name": "via1",
                "layer": "3/0",
                "between": ["met1", "met2"],
                "resistance_ohm": 5.0,
            }
        ],
        "pads": pads,
    }
    if current_model is not None:
        spec["current_model"] = current_model
    path.write_text(json.dumps(spec))


def _island(report, net, island_id):
    net_entry = next(
        entry for entry in report["ir_drop_map"]["nets"] if entry["net"] == net
    )
    return next(
        island for island in net_entry["islands"] if island["island_id"] == island_id
    )


def _pad_and_load_report(tmp_path, *, ground_net=None):
    """The `_basic_fixture` VPWR rail at y=0.5 um (`x: [0, 10]`, one 1.0 ohm
    met1 edge), fed at its left end and loaded with 1 mA at its right end."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "ir.power.json"
    _basic_fixture(gds)
    instance = {"name": "u1", "x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}
    if ground_net is not None:
        instance["ground_net"] = ground_net
    _ir_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8},
            {"name": "vss", "net": "VGND", "x_um": 30.0, "y_um": 0.5, "voltage_v": 0.0},
        ],
        current_model={"supply_net": "VPWR", "instances": [instance]},
    )
    return run_power(str(gds), str(spec))


def test_no_pads_and_no_current_model_means_no_solve(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_power(str(gds), str(spec))
    assert report["ir_drop_map"] is None
    assert report["worst_case_droop_mv"] is None
    assert report["em_verdict"] is None


def test_point_load_droop_is_ohms_law_end_to_end(tmp_path):
    """1 mA drawn through a 1.0 ohm rail is exactly 1.0 mV of droop -- the
    whole path from GDS geometry to reported millivolts, with no golden
    blob in between."""
    report = _pad_and_load_report(tmp_path)

    assert report["worst_case_droop_mv"] == pytest.approx(1.0)
    worst = report["ir_drop_map"]["worst_case"]
    assert worst["net"] == "VPWR"
    assert worst["layer"] == "met1"
    assert worst["x_um"] == pytest.approx(10.0)
    assert worst["voltage_v"] == pytest.approx(1.799)

    island = _island(report, "VPWR", worst["island_id"])
    assert island["solved"] is True
    assert island["unsolved_reason"] is None
    assert island["pad_count"] == 1
    assert island["instance_count"] == 1
    assert island["reference_voltage_v"] == pytest.approx(1.8)
    # Conservation: the pad sources exactly what the instance draws.
    assert island["pad_current_a"] == pytest.approx(1e-3)
    assert island["edges"][0]["current_a"] == pytest.approx(1e-3)

    nodes = {node["id"]: node for node in island["nodes"]}
    pad_node = next(n for n in island["nodes"] if n["pad_voltage_v"] is not None)
    assert pad_node["droop_mv"] == pytest.approx(0.0)
    assert nodes[worst["node_id"]]["injected_current_a"] == pytest.approx(-1e-3)


def test_ground_return_bounces_above_the_ground_pad(tmp_path):
    """With a `ground_net`, the same instance *returns* its current into
    VGND, so VGND's nodes sit above its 0 V pad (ground bounce) -- and the
    reported droop is that magnitude."""
    report = _pad_and_load_report(tmp_path, ground_net="VGND")

    vgnd = next(
        entry for entry in report["ir_drop_map"]["nets"] if entry["net"] == "VGND"
    )
    assert vgnd["instance_count"] == 1
    assert vgnd["current_a"] == pytest.approx(1e-3)
    # The VGND rail is x: [20, 30] um: the return snaps to its near end
    # (20, 0.5) and the pad sits at the far end, so the whole 1.0 ohm rail
    # carries the return current: 1 mV of bounce.
    assert vgnd["worst_case_droop_mv"] == pytest.approx(1.0)
    assert vgnd["worst_case_node"]["voltage_v"] == pytest.approx(0.001)

    island = _island(report, "VGND", vgnd["worst_case_node"]["island_id"])
    assert island["pad_current_a"] == pytest.approx(-1e-3)
    assert island["nodes"][0]["injected_current_a"] == pytest.approx(1e-3)


# --- Activity-weighted current mode (issue #1320, epic #712 Phase 2) -------
#
# Additive to the static `current_a` model above: a `current_model.instances[]`
# entry may set an `activity` object instead -- `toggle_rate_hz *
# capacitance_f * vdd_v`, a synthesis/P&R switching-activity estimate.


def _activity_pad_and_load_report(tmp_path, *, activity, model_vdd_v=None):
    """The same `_basic_fixture` VPWR rail `_pad_and_load_report` loads with a
    static 1 mA, but with the load computed from an `activity` object
    instead."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "ir.power.json"
    _basic_fixture(gds)
    instance = {"name": "u1", "x_um": 10.0, "y_um": 0.5, "activity": activity}
    current_model = {"supply_net": "VPWR", "instances": [instance]}
    if model_vdd_v is not None:
        current_model["vdd_v"] = model_vdd_v
    _ir_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8},
            {"name": "vss", "net": "VGND", "x_um": 30.0, "y_um": 0.5, "voltage_v": 0.0},
        ],
        current_model=current_model,
    )
    return run_power(str(gds), str(spec))


def test_activity_weighted_current_matches_hand_computed_value(tmp_path):
    """Hand-computed: a node toggling at 500 MHz, switching 1 pF at 2.0 V,
    draws an average current of `toggle_rate_hz * capacitance_f * vdd_v` =
    `500e6 * 1e-12 * 2.0` = 1e-3 A -- exactly the static 1 mA
    `_pad_and_load_report` already exercises on this same 1.0 ohm rail, so
    the two current models must agree on the reported droop bit for bit."""
    activity = {"toggle_rate_hz": 500e6, "capacitance_f": 1e-12, "vdd_v": 2.0}
    expected_current_a = (
        activity["toggle_rate_hz"] * activity["capacitance_f"] * activity["vdd_v"]
    )
    assert expected_current_a == pytest.approx(1e-3)

    report = _activity_pad_and_load_report(tmp_path, activity=activity)

    assert report["ir_drop_map"]["total_current_a"] == pytest.approx(expected_current_a)
    assert report["worst_case_droop_mv"] == pytest.approx(1.0)
    worst = report["ir_drop_map"]["worst_case"]
    island = _island(report, "VPWR", worst["island_id"])
    assert island["pad_current_a"] == pytest.approx(expected_current_a)
    assert island["edges"][0]["current_a"] == pytest.approx(expected_current_a)


def test_activity_weighted_current_with_fractional_inputs(tmp_path):
    """A second hand-computed example with non-round numbers: 250 MHz *
    0.4 pF * 0.9 V = 9e-5 A, checked against the same closed-form Ohm's-law
    droop (`I * R`) on the rail's single 1.0 ohm edge."""
    activity = {"toggle_rate_hz": 250e6, "capacitance_f": 0.4e-12, "vdd_v": 0.9}
    expected_current_a = 250e6 * 0.4e-12 * 0.9
    assert expected_current_a == pytest.approx(9e-5)

    report = _activity_pad_and_load_report(tmp_path, activity=activity)

    assert report["ir_drop_map"]["total_current_a"] == pytest.approx(expected_current_a)
    # Ohm's law on the rail's single 1.0 ohm edge: droop_mv = I * R * 1000.
    assert report["worst_case_droop_mv"] == pytest.approx(
        expected_current_a * 1.0 * 1e3
    )


def test_activity_current_falls_back_to_model_level_vdd_v_default(tmp_path):
    """An instance's `activity` may omit `vdd_v` entirely and inherit
    `current_model.vdd_v` -- the same per-model-default-with-per-instance-
    override convention `supply_net`/`ground_net` already use."""
    report = _activity_pad_and_load_report(
        tmp_path,
        activity={"toggle_rate_hz": 500e6, "capacitance_f": 1e-12},
        model_vdd_v=2.0,
    )

    assert report["ir_drop_map"]["total_current_a"] == pytest.approx(1e-3)
    assert report["worst_case_droop_mv"] == pytest.approx(1.0)


def test_activity_instance_vdd_v_overrides_model_default(tmp_path):
    """A per-instance `activity.vdd_v` wins over `current_model.vdd_v` --
    same override precedence as `supply_net`/`ground_net`."""
    report = _activity_pad_and_load_report(
        tmp_path,
        activity={"toggle_rate_hz": 500e6, "capacitance_f": 1e-12, "vdd_v": 2.0},
        model_vdd_v=99.0,
    )

    assert report["ir_drop_map"]["total_current_a"] == pytest.approx(1e-3)


def test_islands_without_a_pad_are_reported_unsolved_with_one_summary_warning(
    tmp_path,
):
    report = _pad_and_load_report(tmp_path)

    unsolved = [
        island
        for entry in report["ir_drop_map"]["nets"]
        for island in entry["islands"]
        if not island["solved"]
    ]
    assert [island["unsolved_reason"] for island in unsolved] == ["no_pad"]
    assert all(node["voltage_v"] is None for node in unsolved[0]["nodes"])
    assert all(edge["current_a"] is None for edge in unsolved[0]["edges"])

    # One aggregate warning, not one per island: a PDN-free routed design has
    # hundreds of these and drowning the report is not a service.
    assert len(report["warnings"]) == 1
    assert "no pad and no modelled current" in report["warnings"][0]
    assert report["ir_drop_map"]["unsolved_current_a"] == 0.0


def test_current_stranded_on_a_padless_island_is_named_and_totalled(tmp_path):
    """A load that lands where no pad can source it is a *modelling* error
    worth naming individually -- unlike a quiet, unloaded, un-strapped
    island."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "ir.power.json"
    _basic_fixture(gds)
    _ir_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        # (5, 10) um is on the met2 stub of the *other* VPWR island, which
        # has no pad of its own.
        current_model={
            "supply_net": "VPWR",
            "instances": [{"name": "u1", "x_um": 5.0, "y_um": 10.0, "current_a": 2e-3}],
        },
    )

    report = run_power(str(gds), str(spec))

    assert report["ir_drop_map"]["unsolved_current_a"] == pytest.approx(2e-3)
    stranded = [w for w in report["warnings"] if "lands there with no pad" in w]
    assert len(stranded) == 1
    assert "2 mA" in stranded[0]
    # Nothing anywhere drooped, because nothing anywhere was solved with a
    # load on it.
    assert report["worst_case_droop_mv"] == pytest.approx(0.0)


def test_pad_on_a_net_with_no_geometry_is_a_warning_not_a_failure(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "ir.power.json"
    _basic_fixture(gds)
    _ir_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8},
            {"name": "nope", "net": "NOPE", "x_um": 0.0, "y_um": 0.0, "voltage_v": 1.8},
        ],
        power_nets=("VPWR", "NOPE"),
    )

    report = run_power(str(gds), str(spec))
    assert any(
        "pad 'nope'" in w and "no extracted geometry" in w for w in report["warnings"]
    )
    nope_pad = next(p for p in report["ir_drop_map"]["pads"] if p["name"] == "nope")
    assert nope_pad["island_id"] is None
    assert nope_pad["node_id"] is None


def test_pads_alone_hold_the_whole_net_at_the_pad_voltage(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "ir.power.json"
    _basic_fixture(gds)
    _ir_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
    )

    report = run_power(str(gds), str(spec))
    assert report["worst_case_droop_mv"] == pytest.approx(0.0)
    assert report["ir_drop_map"]["instance_count"] == 0
    island = _island(report, "VPWR", "VPWR#0")
    assert all(node["voltage_v"] == pytest.approx(1.8) for node in island["nodes"])


# --- Static IR-drop: spec validation ---------------------------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s.update(pads={"net": "VPWR"}), "'pads' must be an array"),
        (lambda s: s.update(pads=["VPWR"]), r"pads\[0\] must be a JSON object"),
        (
            lambda s: s.update(pads=[{"x_um": 0, "y_um": 0, "voltage_v": 1}]),
            "missing 'net'",
        ),
        (
            lambda s: s.update(
                pads=[{"net": "NOPE", "x_um": 0, "y_um": 0, "voltage_v": 1}]
            ),
            "is not one of 'power_nets'",
        ),
        (
            lambda s: s.update(pads=[{"net": "VPWR", "x_um": 0, "y_um": 0}]),
            "missing 'voltage_v'",
        ),
        (
            lambda s: s.update(
                pads=[{"net": "VPWR", "x_um": "left", "y_um": 0, "voltage_v": 1}]
            ),
            "x_um must be a number",
        ),
        (
            lambda s: s.update(
                pads=[
                    {"name": "p", "net": "VPWR", "x_um": 0, "y_um": 0, "voltage_v": 1},
                    {"name": "p", "net": "VPWR", "x_um": 1, "y_um": 0, "voltage_v": 1},
                ]
            ),
            "duplicate pad name",
        ),
        (
            lambda s: s.update(current_model=[{"current_a": 1}]),
            "'current_model' must be a JSON object",
        ),
        (
            lambda s: s.update(current_model={"supply_net": "VPWR"}),
            "must have a non-empty 'instances' array",
        ),
        (
            lambda s: s.update(
                current_model={"instances": [{"x_um": 0, "y_um": 0, "current_a": 1e-3}]}
            ),
            "sets no default",
        ),
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "VPWR",
                    "instances": [{"x_um": 0, "y_um": 0, "current_a": -1e-3}],
                }
            ),
            "current_a must be >= 0",
        ),
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "VPWR",
                    "ground_net": "VPWR",
                    "instances": [{"x_um": 0, "y_um": 0, "current_a": 1e-3}],
                }
            ),
            "draws from and returns to the same net",
        ),
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "NOPE",
                    "instances": [{"x_um": 0, "y_um": 0, "current_a": 1e-3}],
                }
            ),
            "current_model.supply_net 'NOPE' is not one of",
        ),
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "VPWR",
                    "instances": [{"x_um": 0, "y_um": 0}],
                }
            ),
            "missing 'current_a'",
        ),
        # --- Activity-weighted current mode (issue #1320, epic #712 Phase 2)
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "VPWR",
                    "instances": [
                        {
                            "x_um": 0,
                            "y_um": 0,
                            "current_a": 1e-3,
                            "activity": {
                                "toggle_rate_hz": 1e6,
                                "capacitance_f": 1e-12,
                                "vdd_v": 1.8,
                            },
                        }
                    ],
                }
            ),
            "sets both 'current_a' and 'activity'",
        ),
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "VPWR",
                    "instances": [
                        {
                            "x_um": 0,
                            "y_um": 0,
                            "activity": {"capacitance_f": 1e-12, "vdd_v": 1.8},
                        }
                    ],
                }
            ),
            r"activity missing 'toggle_rate_hz'",
        ),
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "VPWR",
                    "instances": [
                        {
                            "x_um": 0,
                            "y_um": 0,
                            "activity": {
                                "toggle_rate_hz": -1e6,
                                "capacitance_f": 1e-12,
                                "vdd_v": 1.8,
                            },
                        }
                    ],
                }
            ),
            "toggle_rate_hz must be >= 0",
        ),
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "VPWR",
                    "instances": [
                        {
                            "x_um": 0,
                            "y_um": 0,
                            "activity": {
                                "toggle_rate_hz": 1e6,
                                "capacitance_f": 1e-12,
                            },
                        }
                    ],
                }
            ),
            r"activity has no 'vdd_v' and 'current_model\.vdd_v' sets no default",
        ),
        (
            lambda s: s.update(
                current_model={
                    "supply_net": "VPWR",
                    "vdd_v": "not-a-number",
                    "instances": [
                        {
                            "x_um": 0,
                            "y_um": 0,
                            "activity": {"toggle_rate_hz": 1e6, "capacitance_f": 1e-12},
                        }
                    ],
                }
            ),
            r"current_model\.vdd_v must be a number",
        ),
    ],
)
def test_ir_spec_validation_errors(tmp_path, mutate, message):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "bad.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)
    document = json.loads(spec.read_text())
    mutate(document)
    spec.write_text(json.dumps(document))

    with pytest.raises(PowerError, match=message):
        run_power(str(gds), str(spec))


# --- Static IR-drop: CLI ----------------------------------------------------


def test_cli_text_output_renders_the_ir_drop_summary(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "ir.power.json"
    _basic_fixture(gds)
    _ir_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}],
        },
    )

    assert main(["power", str(gds), str(spec)]) == 4
    out = capsys.readouterr().out
    assert "ir drop: 1 instance(s) drawing 1 mA through 1 pad(s)" in out
    assert "worst-case droop: 1 mV at VPWR" in out
    assert "net VPWR: 1/2 island(s) solved, worst droop 1 mV" in out


def test_cli_text_output_has_no_ir_section_without_a_solve(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["power", str(gds), str(spec)]) == 4
    assert "ir drop:" not in capsys.readouterr().out


def test_cli_json_ir_drop_map_shape(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "ir.power.json"
    _basic_fixture(gds)
    _ir_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}],
        },
    )

    assert main(["power", str(gds), str(spec), "--format", "json"]) == 4
    data = json.loads(capsys.readouterr().out)

    ir_drop = data["ir_drop_map"]
    assert set(ir_drop.keys()) == {
        "pads",
        "instance_count",
        "total_current_a",
        "unsolved_current_a",
        "solved_node_count",
        "unsolved_node_count",
        "worst_case",
        "nets",
    }
    assert set(ir_drop["nets"][0].keys()) == {
        "net",
        "pad_count",
        "instance_count",
        "current_a",
        "solved_island_count",
        "unsolved_island_count",
        "worst_case_droop_mv",
        "worst_case_node",
        "islands",
    }
    assert set(ir_drop["nets"][0]["islands"][0].keys()) == {
        "island_id",
        "solved",
        "unsolved_reason",
        "pad_count",
        "instance_count",
        "current_a",
        "reference_voltage_v",
        "pad_current_a",
        "solved_node_count",
        "unsolved_node_count",
        "worst_case_droop_mv",
        "worst_case_node_id",
        "iterations",
        "residual",
        "nodes",
        "edges",
    }
    assert set(ir_drop["nets"][0]["islands"][0]["nodes"][0].keys()) == {
        "id",
        "voltage_v",
        "droop_mv",
        "injected_current_a",
        "pad_voltage_v",
    }
    assert set(ir_drop["nets"][0]["islands"][0]["edges"][0].keys()) == {
        "id",
        "current_a",
    }
    assert data["worst_case_droop_mv"] == pytest.approx(1.0)


# --- Acceptance: static IR drop on real klt par output ----------------------


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)
def test_gcd_fixture_solves_for_a_real_ir_drop_map(tmp_path):
    """The full path on a real routed design: extract the GCD macro's power
    grid, feed every rail from its own left-hand end, hang a 0.2 mA load on
    each VPWR rail's far end, and solve.

    Every rail is 0.125 ohm/sq met1 (a sky130-order sheet resistance), so a
    rail of aspect ratio `L/W` squares droops `0.2 mA * 0.125 * L/W`. The
    assertions below are bounds implied by that arithmetic plus conservation,
    not captured output. The independent ngspice cross-check of this same
    design lives in `tests/test_power_ir_cross_check.py`.
    """
    base = {
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
    probe = tmp_path / "gcd.probe.json"
    probe.write_text(json.dumps(base))
    extracted = run_power(str(PLACE_AND_ROUTE_GDS), str(probe))

    pads = []
    instances = []
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
    spec.write_text(
        json.dumps(
            {
                **base,
                "pads": pads,
                "current_model": {
                    "supply_net": "VPWR",
                    "ground_net": "VGND",
                    "instances": instances,
                },
            }
        )
    )
    report = run_power(str(PLACE_AND_ROUTE_GDS), str(spec))
    ir_drop = report["ir_drop_map"]

    # Every island got a pad, so every node has an operating point and no
    # current is stranded.
    assert ir_drop["unsolved_node_count"] == 0
    assert ir_drop["solved_node_count"] == report["node_count"]
    assert ir_drop["unsolved_current_a"] == 0.0
    assert report["warnings"] == []

    # 17 VPWR islands (re-pinned for #1443, see
    # `test_gcd_fixture_extracts_a_real_power_grid`'s docstring) each
    # drawing 0.2 mA.
    assert ir_drop["total_current_a"] == pytest.approx(17 * 2e-4)
    for net_entry in ir_drop["nets"]:
        assert net_entry["unsolved_island_count"] == 0
        # Conservation, per island: the pads source exactly what the model
        # draws (sign flips on the ground net, which sinks the return).
        for island in net_entry["islands"]:
            expected = island["current_a"] * (1 if net_entry["net"] == "VPWR" else -1)
            assert island["pad_current_a"] == pytest.approx(expected, abs=1e-12)

    # A real, non-trivial droop, and a physically bounded one: no rail here
    # is more than ~500 squares, so 0.2 mA * 0.125 ohm/sq * 500 = 12.5 mV is
    # a generous ceiling.
    assert 0.1 < report["worst_case_droop_mv"] < 12.5
    assert report["worst_case_droop_mv"] == max(
        island["worst_case_droop_mv"]
        for net_entry in ir_drop["nets"]
        for island in net_entry["islands"]
    )


# --- EM current-density verdict (issue #846, Phase 1c) ---------------------
#
# `current_limit_a_per_um`/`current_limit_a` are declared the same way
# `sheet_resistance_ohm_per_sq`/`resistance_ohm` already are -- per
# `stackup`/`vias` role, in the spec, citable via `current_limit_source`.
# The values below (`2.8`/`0.29` mA per um/via) are real sky130
# `DCCURRENTDENSITY` numbers, verified against a real sky130A install
# (volare) -- see `sky130_fd_sc_hd__nom.tlef`'s `met1`/`via` `LAYER` blocks
# (Apache-2.0, no NDA'd data) -- but the module itself hardcodes nothing
# PDK-specific: any of these tests would work with any numbers.


def _em_spec(
    path,
    *,
    pads,
    current_model=None,
    power_nets=("VPWR", "VGND"),
    met1_current_limit_a_per_um=None,
    met2_current_limit_a_per_um=None,
    current_limit_source=None,
    via_current_limit_a=None,
) -> None:
    met1 = {
        "name": "met1",
        "layer": "1/0",
        "label_layer": "1/5",
        "sheet_resistance_ohm_per_sq": 0.1,
    }
    if met1_current_limit_a_per_um is not None:
        met1["current_limit_a_per_um"] = met1_current_limit_a_per_um
    if current_limit_source is not None:
        met1["current_limit_source"] = current_limit_source

    met2 = {"name": "met2", "layer": "2/0", "sheet_resistance_ohm_per_sq": 0.05}
    if met2_current_limit_a_per_um is not None:
        met2["current_limit_a_per_um"] = met2_current_limit_a_per_um

    via1 = {
        "name": "via1",
        "layer": "3/0",
        "between": ["met1", "met2"],
        "resistance_ohm": 5.0,
    }
    if via_current_limit_a is not None:
        via1["current_limit_a"] = via_current_limit_a

    spec = {
        "power_nets": list(power_nets),
        "stackup": [met1, met2],
        "vias": [via1],
        "pads": pads,
    }
    if current_model is not None:
        spec["current_model"] = current_model
    path.write_text(json.dumps(spec))


def test_stackup_current_limit_is_scaled_by_rail_width_at_extraction(tmp_path):
    """Even with no `pads`/`current_model` (extraction only, no solve), the
    base network's own edges already carry their scaled `current_limit_a` --
    Phase 1a extraction, not Phase 1c's verdict, does the width scaling."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "em.extract.power.json"
    _basic_fixture(gds)
    _em_spec(
        spec,
        pads=[],
        power_nets=("VPWR",),
        met1_current_limit_a_per_um=0.01,
        current_limit_source="unit-test synthetic limit",
    )

    report = run_power(str(gds), str(spec))
    assert report["em_verdict"] is None  # no solve requested at all

    vpwr = next(entry for entry in report["networks"] if entry["net"] == "VPWR")
    island_a = next(i for i in vpwr["islands"] if i["node_count"] == 2)
    edge = island_a["edges"][0]
    # met1's rail is 1 um wide (x: [0, 10], y: [0, 1]): 0.01 A/um * 1 um.
    assert edge["current_limit_a"] == pytest.approx(0.01)
    assert edge["current_limit_source"] == "unit-test synthetic limit"

    # met2 declared no limit at all -- null, not a default/inherited value.
    island_b = next(i for i in vpwr["islands"] if i["node_count"] == 4)
    met2_edge = next(
        e for e in island_b["edges"] if e["kind"] == "metal" and e["layer"] == "met2"
    )
    assert met2_edge["current_limit_a"] is None
    assert met2_edge["current_limit_source"] is None
    # The via role declared no `current_limit_a` either.
    via_edge = next(e for e in island_b["edges"] if e["kind"] == "via")
    assert via_edge["current_limit_a"] is None


def test_em_verdict_golden_pass_under_the_limit(tmp_path):
    """1 mA through a 1-um-wide met1 rail against a 10 mA limit: comfortably
    under -- the golden *pass* segment.

    Only 1 of the design's 5 total edges is actually checked (the rest have
    no pad/solved current at all), so the *overall* rollup is
    `"pass_partial"` (issue #1997), not a plain `"pass"` -- a genuinely
    complete check is exercised separately by
    `test_gcd_fixture_em_verdict_passes_on_real_rails`/
    `test_modexp_canary_em_verdict_passes_on_real_rails` below. The
    per-net `VPWR` status stays plain `"pass"`: it is unaffected by this
    issue, which only changes the overall rollup's own `status`.
    """
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "em.pass.power.json"
    _basic_fixture(gds)
    _em_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}],
        },
        met1_current_limit_a_per_um=0.01,  # 0.01 A/um * 1 um = 10 mA limit
        current_limit_source="unit-test synthetic limit",
    )

    report = run_power(str(gds), str(spec))
    em = report["em_verdict"]
    assert em is not None
    assert em["status"] == "pass_partial"
    assert em["fail_count"] == 0
    # Island A's one edge (the only solved one) is checked; island B's three
    # edges (unsolved -- no pad) and island C's one edge (VGND, unsolved) are
    # not, regardless of any declared limit.
    assert em["checked_edge_count"] == 1
    assert em["unchecked_edge_count"] == 4

    worst = em["worst_case"]
    assert worst["net"] == "VPWR"
    assert worst["kind"] == "metal"
    assert worst["layer"] == "met1"
    assert worst["current_a"] == pytest.approx(1e-3)
    assert worst["current_limit_a"] == pytest.approx(0.01)
    assert worst["current_limit_source"] == "unit-test synthetic limit"
    assert worst["margin_a"] == pytest.approx(0.009)
    assert worst["status"] == "pass"

    vpwr = next(n for n in em["nets"] if n["net"] == "VPWR")
    assert vpwr["status"] == "pass"
    assert vpwr["checked_edge_count"] == 1
    assert vpwr["fail_count"] == 0
    assert vpwr["failing_edges"] == []

    vgnd = next(n for n in em["nets"] if n["net"] == "VGND")
    assert vgnd["status"] == "not_checked"
    assert vgnd["checked_edge_count"] == 0
    assert vgnd["worst_case"] is None

    # No failure means no extra warning.
    assert not any("current-density limit" in w for w in report["warnings"])


def test_em_verdict_golden_fail_over_the_limit(tmp_path):
    """The same 1 mA through the same rail against a 0.5 mA limit: clearly
    over -- the golden *fail* segment."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "em.fail.power.json"
    _basic_fixture(gds)
    _em_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}],
        },
        met1_current_limit_a_per_um=5e-4,  # 5e-4 A/um * 1 um = 0.5 mA limit
        current_limit_source="unit-test synthetic limit",
    )

    report = run_power(str(gds), str(spec))
    em = report["em_verdict"]
    assert em["status"] == "fail"
    assert em["fail_count"] == 1
    assert em["checked_edge_count"] == 1

    worst = em["worst_case"]
    assert worst["current_a"] == pytest.approx(1e-3)
    assert worst["current_limit_a"] == pytest.approx(5e-4)
    assert worst["margin_a"] == pytest.approx(-5e-4)
    assert worst["status"] == "fail"

    vpwr = next(n for n in em["nets"] if n["net"] == "VPWR")
    assert vpwr["status"] == "fail"
    assert vpwr["fail_count"] == 1
    assert len(vpwr["failing_edges"]) == 1
    failing = vpwr["failing_edges"][0]
    assert failing["kind"] == "metal"
    assert failing["layer"] == "met1"
    assert failing["current_limit_source"] == "unit-test synthetic limit"
    assert failing["status"] == "fail"

    assert any(
        "1 of 1" in w and "current-density limit" in w for w in report["warnings"]
    )


def test_em_verdict_via_edge_over_limit_fails(tmp_path):
    """A via's EM limit is a flat per-shape amperage (no width term) --
    exercised on island B's met1 -> via -> met2 path."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "em.via.power.json"
    _basic_fixture(gds)
    _em_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 5.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 5.0, "y_um": 10.0, "current_a": 1e-3}],
        },
        power_nets=("VPWR",),
        via_current_limit_a=5e-4,  # 0.5 mA limit; 1 mA actual -> fail
    )

    report = run_power(str(gds), str(spec))
    em = report["em_verdict"]
    assert em["status"] == "fail"
    assert em["fail_count"] == 1

    vpwr = next(n for n in em["nets"] if n["net"] == "VPWR")
    via_fail = next(e for e in vpwr["failing_edges"] if e["kind"] == "via")
    assert via_fail["layer"] == "via1"
    assert via_fail["current_a"] == pytest.approx(1e-3)
    assert via_fail["current_limit_a"] == pytest.approx(5e-4)


def test_em_verdict_not_checked_without_any_declared_limit(tmp_path):
    """A spec that declares no `current_limit_a_per_um`/`current_limit_a` at
    all still gets an `em_verdict` (there was a solve), but nothing can be
    checked -- `"not_checked"`, never guessed at."""
    report = _pad_and_load_report(tmp_path)

    em = report["em_verdict"]
    assert em is not None
    assert em["status"] == "not_checked"
    assert em["checked_edge_count"] == 0
    assert em["fail_count"] == 0
    assert em["worst_case"] is None
    for net_entry in em["nets"]:
        assert net_entry["status"] == "not_checked"


def test_em_overall_status_matches_the_common_coverage_rollup(tmp_path):
    """Issue #2116: `em_verdict["status"]`/`report["status"]` are not an
    independently-maintained rule -- they are exactly
    `rollup_status(coverage_rollup({"coverage": report["coverage"]},
    failed=...))`, the same #2109 decision table every other coverage-
    adopting `klt` verb applies. Walks all four reachable rows: zero checked
    edges, partial requested-net coverage, a fully-checked success, and an
    actual EM failure."""
    from klayout_tools.coverage import coverage_rollup, rollup_status

    def _expect(report: dict) -> None:
        em = report["em_verdict"]
        failed = bool(em and em["fail_count"])
        expected = rollup_status(
            coverage_rollup({"coverage": report["coverage"]}, failed=failed),
            success="pass",
        )
        assert report["status"] == expected
        if em is not None:
            assert em["status"] == expected

    # Zero: a solve ran, but no stackup/via role declared any limit at all.
    zero_report = _pad_and_load_report(tmp_path)
    assert zero_report["status"] == "not_checked"
    _expect(zero_report)

    # Partial: one edge checked clean, several others never checked at all.
    gds = tmp_path / "partial.gds"
    partial_spec = tmp_path / "em.status.partial.power.json"
    _basic_fixture(gds)
    _em_spec(
        partial_spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}],
        },
        met1_current_limit_a_per_um=0.01,
    )
    partial_report = run_power(str(gds), str(partial_spec))
    assert partial_report["status"] == "pass_partial"
    _expect(partial_report)

    # Full: both VPWR islands solved, every declared metal/via role has a
    # limit, so every extracted edge on the requested net is checked.
    full_gds = tmp_path / "full.gds"
    full_spec = tmp_path / "em.status.full.power.json"
    _basic_fixture(full_gds)
    _em_spec(
        full_spec,
        power_nets=("VPWR",),
        pads=[
            {"net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8},
            {"net": "VPWR", "x_um": 0.0, "y_um": 5.5, "voltage_v": 1.8},
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [
                {"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3},
                {"x_um": 10.0, "y_um": 5.5, "current_a": 1e-3},
            ],
        },
        met1_current_limit_a_per_um=0.01,
        met2_current_limit_a_per_um=0.01,
        via_current_limit_a=0.01,
    )
    full_report = run_power(str(full_gds), str(full_spec))
    assert full_report["status"] == "pass"
    assert full_report["em_verdict"]["unchecked_edge_count"] == 0
    _expect(full_report)

    # Fail: the same golden-fail rail as `test_em_verdict_golden_fail_over_
    # the_limit` above -- a real EM violation always wins over coverage.
    fail_gds = tmp_path / "fail.gds"
    fail_spec = tmp_path / "em.status.fail.power.json"
    _basic_fixture(fail_gds)
    _em_spec(
        fail_spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}],
        },
        met1_current_limit_a_per_um=5e-4,
    )
    fail_report = run_power(str(fail_gds), str(fail_spec))
    assert fail_report["status"] == "fail"
    _expect(fail_report)


# --- EM current-density verdict: CLI ----------------------------------------


def test_cli_text_output_renders_the_em_verdict_summary(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "em.fail.power.json"
    _basic_fixture(gds)
    _em_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}],
        },
        met1_current_limit_a_per_um=5e-4,
        current_limit_source="unit-test synthetic limit",
    )

    assert main(["power", str(gds), str(spec)]) == 3
    out = capsys.readouterr().out
    assert "em verdict: FAIL" in out
    assert "net VPWR: fail" in out
    assert "unit-test synthetic limit" in out


def test_cli_text_output_has_no_em_section_without_a_solve(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["power", str(gds), str(spec)]) == 4
    assert "em verdict:" not in capsys.readouterr().out


def test_cli_json_em_verdict_shape(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "em.pass.power.json"
    _basic_fixture(gds)
    _em_spec(
        spec,
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 1e-3}],
        },
        met1_current_limit_a_per_um=0.01,
    )

    assert main(["power", str(gds), str(spec), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    em = data["em_verdict"]
    assert set(em.keys()) == {
        "status",
        "checked_edge_count",
        "unchecked_edge_count",
        "fail_count",
        "worst_case",
        "nets",
    }
    assert set(em["nets"][0].keys()) == {
        "net",
        "status",
        "checked_edge_count",
        "unchecked_edge_count",
        "fail_count",
        "worst_case",
        "failing_edges",
    }
    worst = em["worst_case"]
    assert set(worst.keys()) == {
        "net",
        "island_id",
        "edge_id",
        "kind",
        "layer",
        "current_a",
        "current_limit_a",
        "current_limit_source",
        "margin_a",
        "status",
    }

    # The base extraction edges also carry the new additive fields.
    edge = data["networks"][0]["islands"][0]["edges"][0]
    assert "current_limit_a" in edge
    assert "current_limit_source" in edge


# --- Acceptance: EM verdict on real klt par output --------------------------


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)
def test_gcd_fixture_em_verdict_passes_on_real_rails(tmp_path):
    """The real, OpenROAD-produced GCD macro's own rail currents (0.2/0.4 mA
    per the IR-drop worked example) are nowhere near sky130's real
    `DCCURRENTDENSITY AVERAGE 2.8` met1/met2 limit (2.8 mA/um, verified
    against a real sky130A install -- `sky130_fd_sc_hd__nom.tlef`'s `met1`
    `LAYER` block) at this macro's modest load and sky130's own minimum
    routing width (0.14 um met1/met2) -- every checked edge passes."""
    base = {
        "power_nets": ["VPWR", "VGND"],
        "stackup": [
            {
                "name": "met1",
                "layer": "68/20",
                "label_layer": "68/5",
                "sheet_resistance_ohm_per_sq": 0.125,
                "current_limit_a_per_um": 0.0028,
                "current_limit_source": (
                    "sky130_fd_sc_hd__nom.tlef met1 DCCURRENTDENSITY AVERAGE 2.8 mA/um"
                ),
            },
            {
                "name": "met2",
                "layer": "69/20",
                "label_layer": "69/5",
                "sheet_resistance_ohm_per_sq": 0.125,
                "current_limit_a_per_um": 0.0028,
                "current_limit_source": (
                    "sky130_fd_sc_hd__nom.tlef met2 DCCURRENTDENSITY AVERAGE 2.8 mA/um"
                ),
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
    probe = tmp_path / "gcd.em.probe.json"
    probe.write_text(json.dumps(base))
    extracted = run_power(str(PLACE_AND_ROUTE_GDS), str(probe))

    pads = []
    instances = []
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

    spec = tmp_path / "gcd.em.power.json"
    spec.write_text(
        json.dumps(
            {
                **base,
                "pads": pads,
                "current_model": {
                    "supply_net": "VPWR",
                    "ground_net": "VGND",
                    "instances": instances,
                },
            }
        )
    )
    report = run_power(str(PLACE_AND_ROUTE_GDS), str(spec))
    em = report["em_verdict"]

    assert em is not None
    assert em["status"] == "pass"
    assert em["fail_count"] == 0
    # Every edge in the design is on a met1/met2 role with a declared limit,
    # and (per the IR-drop acceptance test above) every node solves -- so
    # every edge is checked, none unchecked.
    assert em["checked_edge_count"] == report["edge_count"]
    assert em["unchecked_edge_count"] == 0


# --- Acceptance: a real fleet canary, not just the `gcd` fixture (#1322) ----
#
# Epic #712's acceptance criterion 3 asks for `klt power` "validated ... on
# a real routed canary (post-#700)", not only the `gcd` corpus fixture every
# test above uses. `sky130-modexp` (`2AMLogic/sky130-modexp`) is that fleet
# canary: a real digital block carried through `klt synthesize` + `klt
# place-and-route` (real OpenROAD) to a routed GDS, independent of this
# repo's own `gcd` worked-example netlist. The three tests below mirror the
# `gcd` acceptance tests above -- extraction, IR-drop solve, EM verdict --
# on `MODEXP_CANARY_GDS`.
#
# The per-instance current model below is grounded in a real measured
# number rather than an arbitrary pick: `sky130-modexp`'s own
# `verification/records/place-and-route/records/20260814-203901-c741877.md`
# reports `estimated power 0.876 mW` for this exact GDS at the nominal
# `tt_025C_1v80` corner (1.8 V). `total_current_a = 0.876 mW / 1.8 V`
# is spread evenly across every `VPWR` island's far end -- the same
# "one load per island" shape the `gcd` worked example uses, except the
# *total* is a real measured figure instead of a hand-picked 0.2 mA per
# island. This is a coarse proxy, not a per-instance activity model: the
# real per-instance switching activity `klt place-and-route`/OpenROAD's STA
# report does not break down per standard cell in a form `klt power`'s
# `current_model.instances[].activity` (issue #1320) can consume today --
# see `docs/cli/power.md`'s "Worked example: a real fleet canary" section
# for this caveat spelled out for a reader who did not read this comment.

_MODEXP_ESTIMATED_POWER_W = 0.876e-3
_MODEXP_VDD_V = 1.8
_MODEXP_BASE_SPEC = {
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


@pytest.mark.skipif(
    not MODEXP_CANARY_GDS.is_file(),
    reason="no vendored sky130-modexp fleet-canary GDS checked in",
)
def test_modexp_canary_extracts_a_real_power_grid(tmp_path):
    """`klt power` extraction-only against the real `sky130-modexp` canary:
    a larger, PDN-free design that resolves to many small disconnected
    islands rather than one mesh -- a regression pin against this specific,
    static, vendored fixture. (Unlike `tests/corpus/place_and_route/gcd.gds.gz`
    above, this `modexp` GDS is a vendored canary, not regenerated by
    `tests/corpus/place_and_route/regenerate.sh` -- #1443's row-rail-fix
    regeneration and #1442's fix itself do not touch this fixture, so its
    island counts below are untouched.)"""
    probe = tmp_path / "modexp.probe.json"
    probe.write_text(json.dumps(_MODEXP_BASE_SPEC))
    report = run_power(str(MODEXP_CANARY_GDS), str(probe))

    assert report["warnings"] == []
    by_net = {n["net"]: n for n in report["networks"]}
    assert by_net["VPWR"]["island_count"] == 216
    assert by_net["VGND"]["island_count"] == 228
    assert report["node_count"] == 888
    assert report["edge_count"] == 444
    assert report["island_count"] == 444


def _modexp_pads_and_vpwr_instances(extracted, *, per_instance_current_a):
    """Build one pad per island (fed from its own left-hand end, held at
    1.8 V/0.0 V) plus one `VPWR`-only load at each island's far end -- the
    same "feed left, load right" shape the `gcd` IR-drop worked example
    (`test_gcd_fixture_solves_for_a_real_ir_drop_map` above) uses."""
    pads = []
    instances = []
    for net_entry in extracted["networks"]:
        for island in net_entry["islands"]:
            first, last = island["nodes"][0], island["nodes"][-1]
            pads.append(
                {
                    "name": f"pad_{island['island_id']}",
                    "net": net_entry["net"],
                    "x_um": first["x_um"],
                    "y_um": first["y_um"],
                    "voltage_v": _MODEXP_VDD_V if net_entry["net"] == "VPWR" else 0.0,
                }
            )
            if net_entry["net"] == "VPWR":
                instances.append(
                    {
                        "name": f"load_{island['island_id']}",
                        "x_um": last["x_um"],
                        "y_um": last["y_um"],
                        "current_a": per_instance_current_a,
                    }
                )
    return pads, instances


@pytest.mark.skipif(
    not MODEXP_CANARY_GDS.is_file(),
    reason="no vendored sky130-modexp fleet-canary GDS checked in",
)
def test_modexp_canary_solves_for_real_ir_drop_map(tmp_path):
    """The full IR-drop solve on the real fleet canary, with the total
    current grounded in `sky130-modexp`'s own measured `estimated power
    0.876 mW` (see module-level comment above for the full derivation)."""
    probe = tmp_path / "modexp.ir.probe.json"
    probe.write_text(json.dumps(_MODEXP_BASE_SPEC))
    extracted = run_power(str(MODEXP_CANARY_GDS), str(probe))

    vpwr_island_count = sum(
        n["island_count"] for n in extracted["networks"] if n["net"] == "VPWR"
    )
    total_current_a = _MODEXP_ESTIMATED_POWER_W / _MODEXP_VDD_V
    per_instance_current_a = total_current_a / vpwr_island_count
    pads, instances = _modexp_pads_and_vpwr_instances(
        extracted, per_instance_current_a=per_instance_current_a
    )

    spec = tmp_path / "modexp.ir.power.json"
    spec.write_text(
        json.dumps(
            {
                **_MODEXP_BASE_SPEC,
                "pads": pads,
                "current_model": {
                    "supply_net": "VPWR",
                    "ground_net": "VGND",
                    "instances": instances,
                },
            }
        )
    )
    report = run_power(str(MODEXP_CANARY_GDS), str(spec))
    ir_drop = report["ir_drop_map"]

    # Every one of the 444 islands got a pad -- nothing stranded.
    assert ir_drop["unsolved_node_count"] == 0
    assert ir_drop["solved_node_count"] == report["node_count"]
    assert ir_drop["unsolved_current_a"] == 0.0
    assert report["warnings"] == []
    assert ir_drop["total_current_a"] == pytest.approx(total_current_a)

    # A real, non-trivial (if tiny -- ~2.25 uA/island vs `gcd`'s hand-picked
    # 0.2 mA/island) droop, well inside a generous physical ceiling: no
    # rail here is more than ~500 squares, so even at 10x this canary's own
    # per-island current, 2.25e-5 A * 0.125 ohm/sq * 500 = 1.4 mV bounds it.
    assert 0.0 < report["worst_case_droop_mv"] < 1.4
    assert report["worst_case_droop_mv"] == pytest.approx(0.04426376, rel=1e-3)


@pytest.mark.skipif(
    not MODEXP_CANARY_GDS.is_file(),
    reason="no vendored sky130-modexp fleet-canary GDS checked in",
)
def test_modexp_canary_em_verdict_passes_on_real_rails(tmp_path):
    """Real sky130 `DCCURRENTDENSITY` limits (2.8 mA/um met1/met2, 0.29 mA
    via -- the same numbers `test_gcd_fixture_em_verdict_passes_on_real_rails`
    above cites) against this canary's own real (tiny) rail currents: every
    checked edge passes comfortably, same conclusion as `gcd`."""
    stackup = [
        {
            **_MODEXP_BASE_SPEC["stackup"][0],
            "current_limit_a_per_um": 0.0028,
            "current_limit_source": (
                "sky130_fd_sc_hd__nom.tlef met1 DCCURRENTDENSITY AVERAGE 2.8 mA/um"
            ),
        },
        {
            **_MODEXP_BASE_SPEC["stackup"][1],
            "current_limit_a_per_um": 0.0028,
            "current_limit_source": (
                "sky130_fd_sc_hd__nom.tlef met2 DCCURRENTDENSITY AVERAGE 2.8 mA/um"
            ),
        },
    ]
    vias = [
        {
            **_MODEXP_BASE_SPEC["vias"][0],
            "current_limit_a": 0.00029,
            "current_limit_source": (
                "sky130_fd_sc_hd__nom.tlef via1 DCCURRENTDENSITY 0.29 mA"
            ),
        }
    ]
    base = {**_MODEXP_BASE_SPEC, "stackup": stackup, "vias": vias}
    probe = tmp_path / "modexp.em.probe.json"
    probe.write_text(json.dumps(base))
    extracted = run_power(str(MODEXP_CANARY_GDS), str(probe))

    vpwr_island_count = sum(
        n["island_count"] for n in extracted["networks"] if n["net"] == "VPWR"
    )
    total_current_a = _MODEXP_ESTIMATED_POWER_W / _MODEXP_VDD_V
    per_instance_current_a = total_current_a / vpwr_island_count
    pads, instances = _modexp_pads_and_vpwr_instances(
        extracted, per_instance_current_a=per_instance_current_a
    )

    spec = tmp_path / "modexp.em.power.json"
    spec.write_text(
        json.dumps(
            {
                **base,
                "pads": pads,
                "current_model": {
                    "supply_net": "VPWR",
                    "ground_net": "VGND",
                    "instances": instances,
                },
            }
        )
    )
    report = run_power(str(MODEXP_CANARY_GDS), str(spec))
    em = report["em_verdict"]

    assert em is not None
    assert em["status"] == "pass"
    assert em["fail_count"] == 0
    assert em["checked_edge_count"] == report["edge_count"]
    assert em["unchecked_edge_count"] == 0


@pytest.mark.skipif(
    not MODEXP_CANARY_GDS.is_file(),
    reason="no vendored sky130-modexp fleet-canary GDS checked in",
)
def test_modexp_canary_klt_signoff_reports_pass(tmp_path):
    """Epic #712's acceptance criterion 3, closed end to end: once `klt
    power`'s IR-drop/EM verdict is a `klt signoff`-consumable evidence kind
    (issue #1321, merged), `klt signoff` reports pass/fail on this real
    fleet canary's own envelope -- not a hand-built fixture, the real
    `run_power` output for `MODEXP_CANARY_GDS`, fed straight into
    `build_signoff`."""
    from klayout_tools.signoff import build_signoff

    stackup = [
        {
            **_MODEXP_BASE_SPEC["stackup"][0],
            "current_limit_a_per_um": 0.0028,
            "current_limit_source": (
                "sky130_fd_sc_hd__nom.tlef met1 DCCURRENTDENSITY AVERAGE 2.8 mA/um"
            ),
        },
        {
            **_MODEXP_BASE_SPEC["stackup"][1],
            "current_limit_a_per_um": 0.0028,
            "current_limit_source": (
                "sky130_fd_sc_hd__nom.tlef met2 DCCURRENTDENSITY AVERAGE 2.8 mA/um"
            ),
        },
    ]
    vias = [
        {
            **_MODEXP_BASE_SPEC["vias"][0],
            "current_limit_a": 0.00029,
            "current_limit_source": (
                "sky130_fd_sc_hd__nom.tlef via1 DCCURRENTDENSITY 0.29 mA"
            ),
        }
    ]
    base = {**_MODEXP_BASE_SPEC, "stackup": stackup, "vias": vias}
    probe = tmp_path / "modexp.signoff.probe.json"
    probe.write_text(json.dumps(base))
    extracted = run_power(str(MODEXP_CANARY_GDS), str(probe))

    vpwr_island_count = sum(
        n["island_count"] for n in extracted["networks"] if n["net"] == "VPWR"
    )
    total_current_a = _MODEXP_ESTIMATED_POWER_W / _MODEXP_VDD_V
    per_instance_current_a = total_current_a / vpwr_island_count
    pads, instances = _modexp_pads_and_vpwr_instances(
        extracted, per_instance_current_a=per_instance_current_a
    )

    spec = tmp_path / "modexp.signoff.power.json"
    spec.write_text(
        json.dumps(
            {
                **base,
                "pads": pads,
                "current_model": {
                    "supply_net": "VPWR",
                    "ground_net": "VGND",
                    "instances": instances,
                },
            }
        )
    )
    report = run_power(str(MODEXP_CANARY_GDS), str(spec))
    report_path = tmp_path / "modexp.power.report.json"
    report_path.write_text(json.dumps(report))

    result = build_signoff([str(report_path)])

    assert result["status"] == "pass"
    assert result["check_count"] == 1
    check = result["checks"][0]
    assert check["kind"] == "power"
    assert check["passed"] is True
    assert check["detail"]["em_verdict_status"] == "pass"
    assert check["detail"]["em_verdict_fail_count"] == 0


# --- devices[]: drawn device bodies are not wires (issue #2260) --------------


def _resistor_string_fixture(path) -> None:
    """A rail-to-rail poly-resistor string: two labelled `met1` supply rails
    joined *only* through a drawn poly resistor body, contacted at each end
    -- the defining topology of a power-on-reset divider, a brown-out
    detector, or a supply-referenced bias string.

    The layout is DRC-plausible and electrically correct: VDD and VSS are
    two distinct nodes with a resistor between them, not a short. But every
    shape on the path is on a declared conductor role, so without a
    `devices[]` declaration `klt power`'s connectivity model reads the
    resistor body as a wire -- and then *solves* it, putting the worst-case
    IR-drop node and the failing EM edge on the far rail. That is the
    reproduction from issue #2260.

    Layer numbers: met1=1/0, met1 label=1/5, poly=4/0, contact=5/0, and the
    PDK device-body marker on 62/0 (gf180mcu's own `Resistor` layer number,
    kept recognisable -- the same number `tests/test_erc.py`'s sibling
    `devices[]` fixture uses).
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")

    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    poly = layout.layer(4, 0)
    contact = layout.layer(5, 0)
    res_marker = layout.layer(62, 0)

    # The two supply rails, on met1, 6 um apart -- no metal runs between them.
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(1)))
    top.shapes(met1_label).insert(kdb.Text("VDD", kdb.Trans(_um(1), _um(0.5))))
    top.shapes(met1).insert(kdb.Box.new(_um(8), _um(0), _um(10), _um(1)))
    top.shapes(met1_label).insert(kdb.Text("VSS", kdb.Trans(_um(9), _um(0.5))))

    # The resistor: one poly bar from rail to rail, contacted at each head.
    top.shapes(poly).insert(kdb.Box.new(_um(1), _um(0.2), _um(9), _um(0.8)))
    top.shapes(contact).insert(kdb.Box.new(_um(1.2), _um(0.3), _um(1.4), _um(0.5)))
    top.shapes(contact).insert(kdb.Box.new(_um(8.6), _um(0.3), _um(8.8), _um(0.5)))

    # The PDK's own device-body marker over the resistor body (not the
    # heads): 6 um x 0.8 um = 4.8 um^2, drawn with the 0.1 um top/bottom
    # enclosure past the 0.6 um-tall poly bar that a PDK device marker
    # conventionally carries -- so only 6 um x 0.6 um = 3.6 um^2 of it is
    # actually subtracted from `poly`.
    top.shapes(res_marker).insert(kdb.Box.new(_um(2), _um(0.1), _um(8), _um(0.9)))

    layout.write(str(path))


#: The `devices[]` declaration matching `_resistor_string_fixture`.
_RESISTOR_DEVICES = [{"name": "poly_resistor", "body_layer": "62/0", "on": "poly"}]

#: What `_RESISTOR_DEVICES` actually subtracts from `poly`: the 6 um x 0.8 um
#: marker (4.8 um^2 of its own) intersected with the 0.6 um-tall poly bar it
#: is drawn over -> 6 um x 0.6 um. The gap between the two numbers is
#: ordinary marker overhang, and reporting the *intersection* is the point --
#: see `test_device_body_area_is_the_intersection_with_its_role`.
_RESISTOR_SUBTRACTED_UM2 = 3.6

#: `_RESISTOR_DEVICES` with the same marker layer additionally declared `on` a
#: role it never touches (the `contact` via role: two 0.2 um x 0.2 um boxes
#: under the resistor heads, outside the marker's own x-range). One correct
#: declaration, one wrong-`on` one.
_RESISTOR_DEVICES_WITH_WRONG_ROLE = [
    {"name": "poly_resistor", "body_layer": "62/0", "on": "poly"},
    {"name": "wrong_role_resistor", "body_layer": "62/0", "on": "contact"},
]


def _resistor_string_spec(devices=None, *, solve=True):
    """The spec for `_resistor_string_fixture`. With `solve` (the default) it
    also declares a VDD pad at the left rail and a 1 mA load at the *right*
    rail -- which is on net VSS, and so must not be reachable from VDD at
    all once the resistor body stops conducting."""
    spec = {
        "power_nets": ["VDD", "VSS"],
        "stackup": [
            {
                "name": "met1",
                "layer": "1/0",
                "label_layer": "1/5",
                "sheet_resistance_ohm_per_sq": 0.1,
                "current_limit_a_per_um": 0.001,
                "current_limit_source": "synthetic fixture",
            },
            {
                "name": "poly",
                "layer": "4/0",
                "sheet_resistance_ohm_per_sq": 50.0,
                "current_limit_a_per_um": 0.001,
                "current_limit_source": "synthetic fixture",
            },
        ],
        "vias": [
            {
                "name": "contact",
                "layer": "5/0",
                "between": ["met1", "poly"],
                "resistance_ohm": 20.0,
                "current_limit_a": 0.001,
                "current_limit_source": "synthetic fixture",
            }
        ],
    }
    if solve:
        spec["pads"] = [
            {
                "name": "vdd_pad",
                "net": "VDD",
                "x_um": 0.5,
                "y_um": 0.5,
                "voltage_v": 1.8,
            }
        ]
        spec["current_model"] = {
            "supply_net": "VDD",
            "instances": [
                {"name": "load", "x_um": 9.5, "y_um": 0.5, "current_a": 0.001}
            ],
        }
    if devices is not None:
        spec["devices"] = devices
    return spec


def _run_resistor_string(tmp_path, devices=None, *, solve=True, name="string"):
    gds = tmp_path / f"{name}.gds"
    spec = tmp_path / f"{name}.power.json"
    _resistor_string_fixture(gds)
    spec.write_text(json.dumps(_resistor_string_spec(devices, solve=solve)))
    return run_power(str(gds), str(spec))


def _island_x_range(network):
    xs = [node["x_um"] for island in network["islands"] for node in island["nodes"]]
    return min(xs), max(xs)


def _network(report, net):
    return next(entry for entry in report["networks"] if entry["net"] == net)


def test_undeclared_device_body_fuses_the_two_supply_rails(tmp_path):
    """The bug, reproduced (issue #2260): with no `devices[]` declaration
    the drawn resistor body conducts, so VDD and VSS resolve to the *same*
    electrical net -- each reported network spans both rails."""
    report = _run_resistor_string(tmp_path, solve=False)

    assert _island_x_range(_network(report, "VDD")) == (0.0, 10.0)
    assert _island_x_range(_network(report, "VSS")) == (0.0, 10.0)
    # Same physical net seen twice, once per requested name.
    assert (
        _network(report, "VDD")["node_count"] == _network(report, "VSS")["node_count"]
    )


def test_declared_device_body_breaks_the_rail_to_rail_string(tmp_path):
    """The fix: declaring the device-body marker subtracts it from `poly`'s
    conductor region before the R network is built, so each supply stays on
    its own rail -- the resistor heads still reach their rail through the
    contacts, but nothing crosses the body."""
    report = _run_resistor_string(tmp_path, devices=_RESISTOR_DEVICES, solve=False)

    assert _island_x_range(_network(report, "VDD")) == (0.0, 2.0)
    assert _island_x_range(_network(report, "VSS")) == (8.0, 10.0)
    # Both rails still extract as one island each -- the carve-out breaks
    # the string, not the rails.
    assert _network(report, "VDD")["island_count"] == 1
    assert _network(report, "VSS")["island_count"] == 1


def test_undeclared_device_body_puts_the_ir_drop_verdict_on_the_other_rail(tmp_path):
    """`klt power` does not merely mislabel the fused net -- it solves it.
    Without the carve-out the worst-case droop node for VDD sits at x=10 um,
    which is the *VSS* rail: a 707 mV verdict computed on a rail that does
    not exist."""
    report = _run_resistor_string(tmp_path)

    worst = report["ir_drop_map"]["worst_case"]
    assert worst["net"] == "VDD"
    assert worst["x_um"] == 10.0
    assert report["worst_case_droop_mv"] > 100.0


def test_declared_device_body_moves_the_ir_drop_verdict_back_to_the_rail(tmp_path):
    """With the body declared, VDD's worst-case droop node is on VDD's own
    rail (x <= 2 um) and the droop collapses to the real rail's own IR --
    the 1 mA load at x=9.5 um is on VSS and no longer sinks through a
    resistor body pretending to be wire."""
    report = _run_resistor_string(tmp_path, devices=_RESISTOR_DEVICES)

    worst = report["ir_drop_map"]["worst_case"]
    assert worst["net"] == "VDD"
    assert worst["x_um"] <= 2.0
    assert report["worst_case_droop_mv"] < 1.0


def test_declared_device_body_changes_the_em_verdict(tmp_path):
    """The EM half of the same verdict: undeclared, the resistor body is an
    EM-checked `poly` edge carrying the whole 1 mA load and failing its
    limit; declared, there is no such edge and no failure."""
    undeclared = _run_resistor_string(tmp_path, name="undeclared")
    declared = _run_resistor_string(
        tmp_path, devices=_RESISTOR_DEVICES, name="declared"
    )

    assert undeclared["em_verdict"]["status"] == "fail"
    assert undeclared["em_verdict"]["fail_count"] == 1
    assert undeclared["em_verdict"]["worst_case"]["layer"] == "poly"
    assert undeclared["status"] == "fail"

    assert declared["em_verdict"]["fail_count"] == 0
    assert declared["em_verdict"]["status"] != "fail"
    assert declared["status"] != "fail"


def test_device_body_subtraction_is_reported(tmp_path):
    """The carved area is echoed per declaration, so a declaration that
    matched nothing is a visible zero rather than a silent no-op. Top-level
    `devices` rather than `klt erc`'s `provenance.devices` only because
    `klt power` has no `provenance` block -- the four fields are identical."""
    report = _run_resistor_string(tmp_path, devices=_RESISTOR_DEVICES, solve=False)

    assert report["devices"] == [
        {
            "name": "poly_resistor",
            "body_layer": "62/0",
            "on": "poly",
            "body_area_um2": _RESISTOR_SUBTRACTED_UM2,
        }
    ]


def test_device_body_area_is_the_intersection_with_its_role(tmp_path):
    """`body_area_um2` is the area this declaration *actually* subtracted --
    `marker & on`'s own conductor region -- not the marker layer's own area
    (the same rule `klt erc` settled in issue #2226).

    The fixture's RES marker is 6 um x 0.8 um (4.8 um^2) drawn over a 0.6
    um-tall poly bar, i.e. with the 0.1 um top/bottom overhang a PDK device
    marker conventionally carries. Only the 6 um x 0.6 um overlap is
    subtracted, so 3.6 is the honest number and 4.8 over-states it."""
    report = _run_resistor_string(tmp_path, devices=_RESISTOR_DEVICES, solve=False)
    entry = report["devices"][0]

    assert entry["body_area_um2"] == 3.6
    assert entry["body_area_um2"] != 4.8


def test_device_body_declared_on_a_role_it_does_not_touch_reports_zero(tmp_path):
    """The same marker layer declared twice, once `on` the role it is drawn
    over and once `on` a role it never touches. Only the first subtracts
    anything, and the report says so -- that is what makes the field usable
    as a wrong-`on` cross-check."""
    report = _run_resistor_string(
        tmp_path, devices=_RESISTOR_DEVICES_WITH_WRONG_ROLE, solve=False
    )
    devices_by_name = {d["name"]: d for d in report["devices"]}

    assert devices_by_name["poly_resistor"]["body_area_um2"] == 3.6
    assert devices_by_name["wrong_role_resistor"]["body_area_um2"] == 0.0
    # The wrong-`on` entry is reported, not dropped, and still names the
    # role it was declared on.
    assert devices_by_name["wrong_role_resistor"]["on"] == "contact"
    # It also changed nothing: the `contact` via role is untouched, so the
    # resistor heads still reach their rails.
    assert _island_x_range(_network(report, "VDD")) == (0.0, 2.0)
    assert _island_x_range(_network(report, "VSS")) == (8.0, 10.0)


def test_device_body_that_misses_its_declared_role_warns_on_stderr(tmp_path, capsys):
    """A marker that *is* drawn on this layout but subtracts nothing from
    the role it was declared `on` is a spec bug, not an unused layer -- so
    it gets a one-line stderr warning as well as a `0.0` in the report. JSON
    goes to stdout only, so the warning cannot corrupt a piped report."""
    _run_resistor_string(
        tmp_path, devices=_RESISTOR_DEVICES_WITH_WRONG_ROLE, solve=False
    )
    err = capsys.readouterr().err

    assert "klt power: warning:" in err
    assert "wrong_role_resistor" in err
    assert "subtracted nothing" in err
    assert "62/0" in err
    # The correct declaration on `poly` is not warned about.
    assert "'poly_resistor'" not in err


def test_device_body_layer_absent_from_layout_subtracts_nothing(tmp_path, capsys):
    """A `body_layer` this stream never carries is not an error (matching
    `stackup`/`vias`' own convention) -- but the measured `body_area_um2`
    says so, instead of leaving a caller to infer that the carve-out bit."""
    report = _run_resistor_string(
        tmp_path,
        devices=[{"name": "poly_resistor", "body_layer": "99/0", "on": "poly"}],
        solve=False,
    )

    assert report["devices"][0]["body_area_um2"] == 0.0
    # Nothing was subtracted, so the two rails are still fused.
    assert _island_x_range(_network(report, "VDD")) == (0.0, 10.0)
    # No stderr warning here: a layer absent from the stream is the
    # documented "this fixture doesn't draw it" case, unlike a marker that
    # is drawn but misses the role it was declared `on`.
    assert "subtracted nothing" not in capsys.readouterr().err


def test_devices_omitted_reports_an_empty_list(tmp_path):
    report = _run_resistor_string(tmp_path, solve=False)

    assert report["devices"] == []


def test_devices_omitted_is_identical_to_an_empty_declaration(tmp_path):
    """Additive-only: a spec with no `devices[]` behaves exactly as one that
    declares an empty array, and neither bumps `schema_version`."""
    omitted = _run_resistor_string(tmp_path, name="omitted")
    empty = _run_resistor_string(tmp_path, devices=[], name="empty")

    assert omitted["schema_version"] == 1
    assert empty["devices"] == []
    for key in ("networks", "ir_drop_map", "em_verdict", "status", "coverage"):
        assert omitted[key] == empty[key]


def test_devices_declaration_does_not_disturb_an_unrelated_layout(tmp_path):
    """A `devices[]` declaration whose marker touches nothing on the
    declared role leaves every other output exactly as it was."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)
    baseline = run_power(str(gds), str(spec))

    spec_dict = json.loads(spec.read_text())
    spec_dict["devices"] = [
        {"name": "poly_resistor", "body_layer": "62/0", "on": "met1"}
    ]
    with_devices = tmp_path / "basic_devices.power.json"
    with_devices.write_text(json.dumps(spec_dict))
    report = run_power(str(gds), str(with_devices))

    assert report["networks"] == baseline["networks"]
    assert report["island_count"] == baseline["island_count"]
    assert report["devices"][0]["body_area_um2"] == 0.0


def test_devices_on_a_via_role_breaks_the_bridge(tmp_path):
    """A `devices[]` entry may name a `vias` role too -- a MiM capacitor
    whose top-plate strap is the drawn via role itself. Declaring the cap
    body stops the via from bridging the two metal roles."""
    report = _run_resistor_string(
        tmp_path,
        devices=[{"name": "poly_resistor", "body_layer": "62/0", "on": "poly"}]
        + [{"name": "contact_cut", "body_layer": "5/0", "on": "contact"}],
        solve=False,
    )

    # Both contacts are gone, so neither resistor head reaches a rail; each
    # supply is now just its own bare met1 rail.
    assert report["devices"][1]["on"] == "contact"
    assert report["devices"][1]["body_area_um2"] > 0.0
    assert _island_x_range(_network(report, "VDD")) == (0.0, 2.0)


# --- devices[] spec validation (issue #2260) ---------------------------------


@pytest.mark.parametrize(
    "devices,message",
    [
        ("not-an-array", "'devices' must be an array"),
        ([[]], "devices[0] must be a JSON object"),
        ([{"on": "poly"}], "devices[0] missing 'body_layer'"),
        ([{"body_layer": "62/0"}], "devices[0] missing 'on'"),
        (
            [{"body_layer": "62", "on": "poly"}],
            "devices[0].body_layer must be '<layer>/<datatype>'",
        ),
        (
            [{"body_layer": "62/0", "on": "nosuchrole"}],
            "devices[0].on must name a 'stackup' or 'vias' entry",
        ),
        (
            [
                {"name": "r", "body_layer": "62/0", "on": "poly"},
                {"name": "r", "body_layer": "62/0", "on": "met1"},
            ],
            "duplicate device name 'r'",
        ),
    ],
)
def test_malformed_devices_declaration_is_rejected(tmp_path, devices, message):
    gds = tmp_path / "string.gds"
    spec = tmp_path / "string.power.json"
    _resistor_string_fixture(gds)
    spec.write_text(json.dumps(_resistor_string_spec(devices, solve=False)))

    with pytest.raises(PowerError) as excinfo:
        run_power(str(gds), str(spec))

    assert message in str(excinfo.value)


def test_devices_entry_name_defaults_to_its_index(tmp_path):
    report = _run_resistor_string(
        tmp_path, devices=[{"body_layer": "62/0", "on": "poly"}], solve=False
    )

    assert report["devices"][0]["name"] == "device0"
