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
PLACE_AND_ROUTE_GDS = CORPUS_DIR / "place_and_route" / "gcd.gds.gz"


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


# --- run_power: non-rectangular polygon fallback ---------------------------


def test_run_power_non_rectangular_segment_is_bbox_approximated_with_warning(tmp_path):
    gds = tmp_path / "lshape.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    met1 = layout.layer(1, 0)
    met1_label = layout.layer(1, 5)
    # Two touching boxes whose union is L-shaped (not a box).
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(5), _um(2)))
    top.shapes(met1).insert(kdb.Box.new(_um(3), _um(2), _um(5), _um(5)))
    top.shapes(met1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(1), _um(1))))
    layout.write(str(gds))

    spec = tmp_path / "lshape.power.json"
    spec.write_text(
        json.dumps(
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
            }
        )
    )

    report = run_power(str(gds), str(spec))
    assert report["island_count"] == 1
    assert any(
        "non-rectangular" in w and "bounding box" in w for w in report["warnings"]
    )


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

    assert main(["power", str(gds), str(spec), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
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

    assert main(["power", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "net VPWR: 2 island(s)" in out
    assert "net VGND: 1 island(s)" in out
    assert "VPWR#0" in out


def test_cli_text_output_renders_warnings(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec, power_nets=("VPWR", "NOPE"))

    assert main(["power", str(gds), str(spec)]) == 0
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
    cell rows themselves contribute -- many disconnected, un-strapped
    per-row islands rather than one connected mesh. The island counts below
    are a regression pin against this specific, static, committed fixture
    (mirroring `test_drc.py`'s own `violation_count == 4` pin) -- not a
    claim about what a real PDN-equipped design should look like.
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
    assert by_net["VPWR"]["island_count"] == 88
    assert by_net["VGND"]["island_count"] == 105
    assert report["island_count"] == 193
    assert report["node_count"] == 386
    assert report["edge_count"] == 193

    # Every rail is a real, positive resistor -- not a silent zero.
    for entry in report["networks"]:
        for island in entry["islands"]:
            for edge in island["edges"]:
                assert edge["resistance_ohm"] > 0
                assert edge["kind"] in {"metal", "via"}


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

    assert main(["power", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "ir drop: 1 instance(s) drawing 1 mA through 1 pad(s)" in out
    assert "worst-case droop: 1 mV at VPWR" in out
    assert "net VPWR: 1/2 island(s) solved, worst droop 1 mV" in out


def test_cli_text_output_has_no_ir_section_without_a_solve(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["power", str(gds), str(spec)]) == 0
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

    assert main(["power", str(gds), str(spec), "--format", "json"]) == 0
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

    # 88 VPWR islands each drawing 0.2 mA.
    assert ir_drop["total_current_a"] == pytest.approx(88 * 2e-4)
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
    under -- the golden *pass* segment."""
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
    assert em["status"] == "pass"
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

    assert main(["power", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "em verdict: FAIL" in out
    assert "net VPWR: fail" in out
    assert "unit-test synthetic limit" in out


def test_cli_text_output_has_no_em_section_without_a_solve(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["power", str(gds), str(spec)]) == 0
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
