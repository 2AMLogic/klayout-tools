"""Synthetic geometry and real KLayout nets for fragmented supplies (#2078)."""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools import lvs


def _rail_request(tmp_path, rows, *, connected=False, deck="sky130", options=None):
    """Each row contains disconnected metal islands, each with its own labels."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("supply_tile")
    layers = ((68, 20), (68, 5)) if deck == "sky130" else ((34, 0), (34, 10))
    metal, labels = (layout.layer(*pair) for pair in layers)
    for row, islands in enumerate(rows):
        y = row * 5000
        for column, names in enumerate(islands):
            x = column * 3000
            top.shapes(metal).insert(kdb.Box(x, y, x + 1000, y + 1000))
            for offset, name in enumerate(names):
                top.shapes(labels).insert(
                    kdb.Text(name, kdb.Trans(x + 100 + offset * 100, y + 500))
                )
        if connected:
            top.shapes(metal).insert(
                kdb.Box(0, y, (len(islands) - 1) * 3000 + 1000, y + 1000)
            )
    gds = tmp_path / "supply.gds"
    layout.write(str(gds))
    reference = tmp_path / "supply.spice"
    names = sorted({name for row in rows for island in row for name in island})
    reference.write_text(f".SUBCKT supply_tile {' '.join(names)}\n.ENDS\n")
    return json.dumps(
        {
            "layout": {"file": str(gds), "deck": deck},
            "reference": {"netlist": str(reference)},
            "options": options or {},
        }
    )


def _findings(report):
    return [
        finding
        for finding in report["mismatches"]
        if finding["category"] == "net.supply_fragmented"
    ]


@pytest.mark.parametrize("deck", ["sky130", "gf180mcu"])
def test_disconnected_supply_islands_are_counted_from_real_nets(tmp_path, deck):
    request = _rail_request(
        tmp_path,
        [[["VPWR"], ["VPWR"], ["VPWR"]], [["VGND"], ["VGND"]]],
        deck=deck,
    )
    report = lvs.run_lvs(request)
    findings = _findings(report)
    assert [
        (f["details"]["supply"], f["details"]["fragment_count"]) for f in findings
    ] == [
        ("VGND", 2),
        ("VPWR", 3),
    ]
    assert all(f["severity"] == "warning" and f["side"] == "layout" for f in findings)
    assert all(
        f["circuit"] == {"layout": "supply_tile", "reference": None} for f in findings
    )
    assert report["category_counts"]["net.supply_fragmented"] == 2
    assert "net.supply_fragmented" not in report["category_error_counts"]


@pytest.mark.parametrize("deck", ["sky130", "gf180mcu"])
def test_connected_grid_with_many_labels_is_one_net_per_supply(tmp_path, deck):
    request = _rail_request(
        tmp_path,
        [[["VPWR"], ["VPWR"], ["VPWR"]], [["VGND"], ["VGND"]]],
        deck=deck,
        connected=True,
    )
    report = lvs.run_lvs(request)
    assert report["status"] == "match"
    assert not _findings(report)


def test_original_labels_count_each_island_once_despite_aliases(tmp_path):
    request = _rail_request(
        tmp_path,
        [[["VPWR", "VPWR", "ALIAS"], ["vpwr"]], [["VPWR$1"]]],
    )
    findings = _findings(lvs.run_lvs(request))
    assert len(findings) == 1
    assert findings[0]["details"] == {"supply": "VPWR", "fragment_count": 2}


def test_repeated_signal_and_literal_suffix_are_not_inferred_supplies(tmp_path):
    request = _rail_request(
        tmp_path,
        [[["SIGNAL"], ["SIGNAL"]], [["VPWR"], ["VPWR$1"]]],
    )
    assert not _findings(lvs.run_lvs(request))


def test_custom_supply_names_replace_defaults_and_replay(tmp_path):
    request = _rail_request(
        tmp_path,
        [[["AVDD"], ["avdd"]], [["VPWR"], ["VPWR"]]],
        options={"supply_nets": ["avdd", "AVDD"]},
    )
    report = lvs.run_lvs(request)
    findings = _findings(report)
    assert [f["details"] for f in findings] == [{"supply": "AVDD", "fragment_count": 2}]
    assert report["options"]["supply_nets"] == ["AVDD"]
    reconstructed = lvs._reconstruct_lvs_request(report)
    assert reconstructed["options"]["supply_nets"] == ["AVDD"]
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report))
    assert lvs.rerun_lvs_report(str(saved))["status"] == "match"


def test_empty_supply_names_disable_and_replay(tmp_path):
    request = _rail_request(
        tmp_path, [[["VPWR"], ["VPWR"]]], options={"supply_nets": []}
    )
    report = lvs.run_lvs(request)
    assert not _findings(report)
    assert report["options"]["supply_nets"] == []
    assert lvs._reconstruct_lvs_request(report)["options"]["supply_nets"] == []


def test_legacy_report_without_supply_option_has_no_spurious_option_drift(tmp_path):
    report = lvs.run_lvs(_rail_request(tmp_path, [[["VPWR"]]]))
    del report["options"]["supply_nets"]
    saved = tmp_path / "legacy.json"
    saved.write_text(json.dumps(report))
    assert lvs.rerun_lvs_report(str(saved))["status"] == "match"


def test_configured_literal_suffix_is_its_own_supply(tmp_path):
    request = _rail_request(
        tmp_path,
        [[["VPWR"], ["VPWR$1"], ["VPWR$1"]]],
        options={"supply_nets": ["VPWR$1"]},
    )
    assert [finding["details"] for finding in _findings(lvs.run_lvs(request))] == [
        {"supply": "VPWR$1", "fragment_count": 2}
    ]


@pytest.mark.parametrize("value", [None, False, "VPWR", {}, [1], [""], [" "]])
def test_invalid_supply_names_are_request_errors(tmp_path, value):
    request = _rail_request(tmp_path, [[["VPWR"]]], options={"supply_nets": value})
    with pytest.raises(lvs.LvsError, match="options.supply_nets"):
        lvs.run_lvs(request)


def _circuit(netlist, name, labels):
    circuit = kdb.Circuit()
    circuit.name = name
    netlist.add(circuit)
    for label in labels:
        circuit.create_net(label)
    return circuit


def test_circuit_scopes_and_repeated_instances_do_not_multiply_fragments():
    from klayout_tools.lvs_supply import supply_fragmentation_findings

    netlist = kdb.Netlist()
    top = _circuit(netlist, "TOP", ["VPWR"])
    child = _circuit(netlist, "CHILD", ["VPWR"])
    top.create_subcircuit(child, "X1")
    top.create_subcircuit(child, "X2")
    _circuit(netlist, "UNSELECTED", ["VPWR", "VPWR"])
    assert supply_fragmentation_findings(top, ["VPWR"]) == []
    child.create_net("VPWR")
    findings = supply_fragmentation_findings(top, ["VPWR"])
    assert len(findings) == 1
    assert findings[0]["circuit"]["layout"] == "CHILD"
    assert findings[0]["details"] == {"supply": "VPWR", "fragment_count": 2}


def test_distinct_zero_cluster_nets_are_not_collapsed():
    from klayout_tools.lvs_supply import supply_fragmentation_findings

    netlist = kdb.Netlist()
    top = _circuit(netlist, "TOP", ["VPWR", "VPWR", "VPWR$1"])
    assert all(net.cluster_id == 0 for net in top.each_net())
    findings = supply_fragmentation_findings(top, ["VPWR"])
    assert findings[0]["details"]["fragment_count"] == 2
