"""Post-route instance pin connectivity from the DEF and final GDS (#6905)."""

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import place_and_route as flow
from klayout_tools import place_and_route_reports as reports
from klayout_tools.cli import main
from klayout_tools.place_and_route_pin_connectivity import (
    apply_pin_connectivity,
    audit_pin_connectivity,
)
from test_place_and_route import (
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)
from test_place_and_route_batch import _run, _status
from test_place_and_route_batch import fake as fake
from test_place_and_route_congestion import _wrap_route_stdout
from test_place_and_route_corners import CORNERS

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

DEFC = """VERSION 5.8 ;
DESIGN gcd ;
NETS 2 ;
- sig ( A I ) ( B I ) ( PIN INPUT ) + ROUTED met1 ( 0 0 ) ( 2000 0 ) ;
- unused ( A Y ) ;
END NETS
END DESIGN
"""


def _stream(path, *, connect, missing_pin=False, pairs=False):
    layout = kdb.Layout()
    top = layout.create_cell("gcd")
    cell = layout.create_cell("logic_buf")
    metal = layout.layer(68, 20)
    label = layout.layer(68, 5)
    cell.shapes(metal).insert(kdb.Box(0, 0, 200, 200))
    if not missing_pin:
        cell.shapes(label).insert(kdb.Text("I", kdb.Trans(100, 100)))
    placements = [("A", 0), ("B", 2000)]
    if pairs:
        placements += [("C", 4000), ("D", 6000)]
        top.shapes(metal).insert(kdb.Box(4050, 20, 6050, 180))
    for inst, x in placements:
        ref = top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(x, 0)))
        ref.set_property(1, inst)
    top.shapes(metal).insert(kdb.Box(-1000, 50, 2100 if connect else 100, 150))
    top.shapes(label).insert(kdb.Text("INPUT", kdb.Trans(-500, 100)))
    layout.write(str(path))


@pytest.mark.parametrize(
    "connect,status,count", [(False, "failed", 1), (True, "ok", 0)]
)
def test_public_route_verdict_uses_physical_islands(
    tmp_path, monkeypatch, capsys, connect, status, count
):
    request = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    _wrap_route_stdout(
        monkeypatch,
        "\n".join(
            [
                reports.ROUTING_COMPLETENESS_BEGIN,
                "KLT_ROUTING_COMPLETENESS signal_nets=1 unrouted=0",
                reports.ROUTING_COMPLETENESS_END,
            ]
        ),
        {},
    )
    stub = flow._merge_def_to_gds

    def merge(**kwargs):
        info = stub(**kwargs)
        Path(kwargs["def_path"]).write_text(DEFC)
        _stream(kwargs["out_path"], connect=connect)
        return info

    monkeypatch.setattr(flow, "_merge_def_to_gds", merge)
    assert main(["place-and-route", request, "--format", "json"]) == (
        0 if connect else 1
    )
    report = json.loads(capsys.readouterr().out)
    comp = report["congestion"]["completeness"]
    assert comp["unrouted_net_count"] == 0
    assert report["stages"][-1]["congestion"] == report["congestion"]
    assert report["status"] == status
    assert comp["partially_routed_net_count"] == count
    assert comp["pin_connectivity"]["status"] == ("checked" if connect else "open")
    if not connect:
        assert comp["pin_connectivity"]["open_nets"][0]["affected_pins"] == [
            {"instance": "B", "pin": "I"}
        ]


def _audit(tmp, definition, **stream_args):
    d, g, lef = tmp / "gcd.def", tmp / "gcd.gds", tmp / "cells.lef"
    d.write_text(definition)
    _stream(g, **stream_args)
    lef.write_text(
        "MACRO logic_buf\n PIN Y\n DIRECTION OUTPUT ;\n END Y\nEND logic_buf\n"
    )
    return audit_pin_connectivity(str(d), str(g), "gcd", "sky130", str(lef))


def test_two_pairs_on_separate_islands(tmp_path):
    defs = DEFC.replace("( PIN INPUT )", "( C I ) ( D I )")
    check = _audit(tmp_path, defs, connect=True, pairs=True)
    assert check["checked_net_count"] == 1
    (net,) = check["open_nets"]
    assert [[t["instance"] for t in group] for group in net["islands"]] == [
        ["A", "B"],
        ["C", "D"],
    ]
    assert len(net["affected_pins"]) == 4


def test_port_only_peer_and_incomplete_evidence_retain_known_open(tmp_path):
    defs = DEFC.replace("( A I ) ", "").replace(
        "- unused ( A Y ) ;", "- missing ( A Q ) ( B Q ) + ROUTED met1 ( 0 0 ) ;"
    )
    check = _audit(tmp_path, defs, connect=False)
    assert check["status"] == "unchecked"
    assert check["open_nets"][0]["affected_pins"] == [{"instance": "B", "pin": "I"}]
    assert check["unresolved_nets"][0]["net"] == "missing"
    stage = {}
    assert apply_pin_connectivity(stage, check)
    congestion = stage["congestion"]
    assert congestion["completeness"]["partially_routed_net_count"] is None
    assert (
        congestion["null_reasons"]["completeness.partially_routed_net_count"]
        == check["reason"]
    )


@pytest.mark.parametrize(
    "definition",
    [
        DEFC.replace("END NETS", ""),
        DEFC.replace("NETS 2", "NETS 3"),
        DEFC.replace("( B I )", "( B )"),
    ],
)
def test_incomplete_def_cannot_pass(tmp_path, definition):
    assert _audit(tmp_path, definition, connect=True)["status"] == "unchecked"


@pytest.mark.parametrize("bridge, expected", [(True, "checked"), (False, "open")])
def test_alternate_access_pad_requires_physical_internal_connection(
    tmp_path, bridge, expected
):
    from klayout_tools.decks import get_extraction_deck

    layout = kdb.Layout()
    deck = get_extraction_deck("gf180mcu")
    top = layout.create_cell("gcd")
    cell = layout.create_cell("inverter")
    metal = layout.layer(*deck.metals[0])
    label = layout.layer(*deck.metal_labels[0])
    contact = layout.layer(*deck.contact)
    poly = layout.layer(*deck.poly)
    for x in [0, 2000]:
        cell.shapes(metal).insert(kdb.Box(x, 0, x + 200, 200))
        cell.shapes(contact).insert(kdb.Box(x + 50, 50, x + 150, 150))
    cell.shapes(poly).insert(kdb.Box(50, 40, 2150 if bridge else 150, 160))
    cell.shapes(label).insert(kdb.Text("I", kdb.Trans(2100, 100)))
    ref = top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans()))
    ref.set_property(1, "B")
    top.shapes(metal).insert(kdb.Box(-1000, 50, 100, 150))
    top.shapes(label).insert(kdb.Text("INPUT", kdb.Trans(-500, 100)))
    gds, definition, lef = (
        tmp_path / name for name in ("gcd.gds", "gcd.def", "cell.lef")
    )
    layout.write(str(gds))
    definition.write_text(DEFC.replace("( A I ) ", ""))
    lef.write_text("")
    check = audit_pin_connectivity(
        str(definition), str(gds), "gcd", "gf180mcu", str(lef)
    )
    assert check["status"] == expected


def test_completed_open_route_retains_diagnostics_and_artifacts(request):
    runner = request.getfixturevalue("fake")
    corner = CORNERS[0]
    outputs = runner.default_outputs(corner)
    report = json.loads(outputs["report.json"])
    check = {
        "status": "open",
        "open_nets": [
            {"net": "data", "affected_pins": [{"instance": "U2", "pin": "I"}]}
        ],
    }
    report.update(
        status="failed",
        stage_reached="route",
        congestion={
            "completeness": {"unrouted_net_count": 0, "pin_connectivity": check}
        },
    )
    outputs["report.json"] = json.dumps(report)
    outputs["artifacts/gcd.gds"] = "retained routed stream"
    runner.outputs[corner] = outputs
    runner.status[corner] = _status("failed", exit_code="1")

    result = _run(runner)
    failed, clean = result["corner_runs"]
    assert result["status"] == failed["status"] == "failed"
    assert failed["congestion"]["completeness"]["pin_connectivity"] == check
    assert (
        Path(failed["output_dir"]) / "gcd.gds"
    ).read_text() == "retained routed stream"
    assert clean["status"] == "ok"


def test_def_and_router_count_disagreement_stays_unchecked(tmp_path):
    check = _audit(tmp_path, DEFC, connect=True)
    stage = {
        "congestion": {
            "completeness": {"signal_net_count": 2, "unrouted_net_count": 0},
            "null_reasons": {},
        }
    }
    assert not apply_pin_connectivity(stage, check)
    comp = stage["congestion"]["completeness"]
    assert comp["pin_connectivity"]["status"] == "unchecked"
    assert "OpenDB counted 2" in comp["pin_connectivity"]["reason"]
    assert comp["partially_routed_net_count"] is None


@pytest.mark.parametrize(
    "geometry", ["label_over_parent_wire", "duplicate_isolated_labels"]
)
def test_ambiguous_master_pin_cannot_be_proved_by_parent_route(tmp_path, geometry):
    gds = tmp_path / "gcd.gds"
    _stream(gds, connect=True)
    layout = kdb.Layout()
    layout.read(str(gds))
    cell = layout.cell("logic_buf")
    metal, label = layout.find_layer(68, 20), layout.find_layer(68, 5)
    if geometry == "label_over_parent_wire":
        cell.shapes(metal).clear()
    else:
        cell.shapes(metal).insert(kdb.Box(500, 0, 700, 200))
        cell.shapes(label).insert(kdb.Text("I", kdb.Trans(600, 100)))
    layout.write(str(gds))
    definition, lef = tmp_path / "gcd.def", tmp_path / "cells.lef"
    definition.write_text(DEFC)
    lef.write_text("")
    result = audit_pin_connectivity(
        str(definition), str(gds), "gcd", "sky130", str(lef)
    )
    assert result["status"] == "unchecked"
    assert result["unresolved_nets"][0]["net"] == "sig"
