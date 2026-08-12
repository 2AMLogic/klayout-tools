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
        "warnings",
    }
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
